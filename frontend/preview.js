// HLS preview playback. The backend proxies the relay's low-resolution operator
// feed at /<channel>/preview/index.m3u8 — a separate path from the program leg,
// so opening a preview never touches what YouTube is reading.

import { getToken } from './api.js';

export class Preview {
  constructor(video, onStatus) {
    this.video = video;
    this.onStatus = onStatus;
    this.hls = null;
    this.channel = null;
    this.engine = '';
    this.detail = '';
    this.recoveries = 0;

    this.video.addEventListener('playing', () => this.#report('playing'));
    this.video.addEventListener('waiting', () => this.#report('buffering'));
    this.video.addEventListener('pause', () => this.#report('paused'));
  }

  url(channel) {
    return `/${encodeURIComponent(channel)}/preview/index.m3u8`;
  }

  start(channel) {
    this.stop();
    this.channel = channel;
    this.recoveries = 0;
    const src = this.url(channel);
    const Hls = window.Hls;

    if (Hls && Hls.isSupported()) {
      this.engine = 'hls.js';
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: false,
        backBufferLength: 30,
        manifestLoadingMaxRetry: 4,
        levelLoadingMaxRetry: 4,
        fragLoadingMaxRetry: 6,
        // hls.js is the only HLS path that can carry the bearer token.
        xhrSetup: (xhr) => {
          const token = getToken();
          if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
        },
      });
      this.hls = hls;
      // Attach first and load on MEDIA_ATTACHED: appending before the MediaSource
      // is open is what produces mediaSourceRequiresReset.
      hls.on(Hls.Events.MEDIA_ATTACHED, () => hls.loadSource(src));
      hls.on(Hls.Events.MANIFEST_PARSED, () => this.#play());
      hls.on(Hls.Events.LEVEL_LOADED, (_event, data) => {
        const level = hls.levels[data.level];
        if (level && level.bitrate) this.detail = `${Math.round(level.bitrate / 1000)} kb/s`;
      });
      hls.on(Hls.Events.ERROR, (_event, data) => this.#onHlsError(Hls, data));
      hls.attachMedia(this.video);
      this.#status('connecting\u2026');
      return;
    }

    if (this.video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari plays HLS natively but cannot attach an Authorization header.
      this.engine = 'native';
      this.video.src = src;
      this.video.addEventListener('loadedmetadata', () => this.#play(), { once: true });
      this.video.addEventListener('error', () => this.#status('native HLS failed \u2014 check the preview path'), { once: true });
      this.#status('connecting\u2026');
      return;
    }

    this.engine = 'none';
    this.#status('no HLS support in this browser');
  }

  stop() {
    if (this.hls) {
      this.hls.destroy();
      this.hls = null;
    }
    this.video.pause();
    if (this.video.getAttribute('src')) {
      this.video.removeAttribute('src');
      this.video.load();
    }
    this.channel = null;
    this.engine = '';
    this.detail = '';
    this.#status('preview stopped');
  }

  setAudio(on) {
    this.video.muted = !on;
    if (on) this.video.volume = 1;
  }

  #play() {
    const p = this.video.play();
    if (p && typeof p.catch === 'function') {
      p.catch(() => this.#status('autoplay blocked \u2014 click the video to start'));
    }
  }

  #report(what) {
    if (!this.engine) return;
    const size = this.video.videoWidth ? `${this.video.videoWidth}\u00D7${this.video.videoHeight}` : '';
    this.#status([size, this.detail, what].filter(Boolean).join(' \u00B7 '));
  }

  #onHlsError(Hls, data) {
    if (!data.fatal) return;
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      const code = data.response && data.response.code;
      if (code === 401 || code === 403) {
        this.#status(`preview refused (${code}) \u2014 check the API token`);
        this.hls.destroy();
        this.hls = null;
        return;
      }
      this.#status(`network error (${data.details}) \u2014 retrying`);
      this.hls.startLoad();
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && this.recoveries < 2) {
      this.recoveries += 1;
      this.#status(`media error (${data.details}) \u2014 recovering`);
      if (this.recoveries > 1) this.hls.swapAudioCodec();
      this.hls.recoverMediaError();
      return;
    }
    // The underlying message is the useful part: a browser without an H.264/AAC
    // decoder fails here and the detail code alone does not say so.
    const cause = data.error && data.error.message ? ` \u2014 ${data.error.message.slice(0, 160)}` : '';
    this.#status(`fatal: ${data.details}${cause}`);
    this.hls.destroy();
    this.hls = null;
  }

  #status(text) {
    this.onStatus(text, this.engine);
  }
}
