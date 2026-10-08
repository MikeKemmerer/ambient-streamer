// Live: what the channel is doing right now, with the controls that interrupt it
// kept behind a hold-to-confirm sheet.

import { Preview } from '../../preview.js';
import { basename, fmtNum, fmtUptime, metricTone } from '../../render.js';
import { confirmLifecycle, skipTrack } from '../data/actions.js';
import {
  channelIsRunning, config, deliveryOf, isStopped, resolutionOf, state, vizEnabled,
} from '../data/store.js';
import {
  armButton, btn, callout, card, chip, copyText, el, switchRow,
} from '../lib/ui.js';
import { liveBox } from './common.js';

const DASH = '\u2014';

function fact(label, valueNode) {
  return el('div', { class: 'fact' }, [el('dt', { class: 'fact__k', text: label }), el('dd', { class: 'fact__v' }, [valueNode])]);
}

function tile(label) {
  const value = el('div', { class: 'tile__v num', text: DASH });
  const root = el('div', { class: 'tile', dataset: { tone: 'idle' } }, [el('div', { class: 'tile__k', text: label }), value]);
  root.set = (text, tone = 'idle') => { value.textContent = text; root.dataset.tone = tone; };
  return root;
}

export function mountLive(host, ctx) {
  const name = ctx.name;

  // --- problems -----------------------------------------------------------
  const problems = liveBox(() => {
    const ch = state.channels.get(name) || {};
    const out = [];
    if (ch.config_error) out.push(callout(`Config: ${ch.config_error}`, 'bad'));
    if (ch.fault) out.push(callout(`${ch.fault}${ch.fault_detail ? ` \u2014 ${ch.fault_detail}` : ''}`, 'bad'));
    const warnings = Array.isArray(ch.warnings) ? ch.warnings : [];
    for (const w of warnings) out.push(callout(String(w), 'warn'));
    const substituted = ch.encoder_requested && ch.encoder && ch.encoder_requested !== ch.encoder;
    if (substituted) out.push(callout(`Encoder substituted: asked for ${ch.encoder_requested}, running ${ch.encoder} (probe failed).`, 'warn'));
    return out;
  }, { cls: 'stack stack--tight' });

  // --- preview (built once: it owns a playing video) -----------------------
  const video = el('video', { class: 'preview__video', playsinline: true, muted: true, disablepictureinpicture: true });
  video.muted = true;
  const statusLine = el('div', { class: 'preview__status', text: 'preview stopped' });
  const engineLine = el('span', { class: 'note', text: '' });
  const preview = new Preview(video, (text, engine) => {
    statusLine.textContent = text;
    engineLine.textContent = engine ? `engine: ${engine}` : '';
  });
  const startPreview = btn({ label: 'Start preview', icon: 'play', variant: 'primary', onClick: () => preview.start(name) });
  const stopPreview = btn({ label: 'Stop', icon: 'stop', variant: 'secondary', onClick: () => preview.stop() });
  const audio = switchRow({
    label: 'Preview sound', hint: 'Plays through this phone only.', checked: false, mut: false,
    onChange: (on) => preview.setAudio(on),
  });
  video.addEventListener('click', () => video.play().catch(() => {}));

  const previewCard = card({
    title: 'Preview',
    subtitle: 'Low-resolution operator feed. It never touches what YouTube receives.',
    children: [
      el('div', { class: 'preview' }, [video, statusLine]),
      el('div', { class: 'row' }, [startPreview, stopPreview]),
      audio,
      engineLine,
    ],
  });

  // --- now on air (built once, updated in place) ---------------------------
  const nowTrack = el('span', { class: 'wrap', text: DASH });
  const nowNext = el('span', { class: 'wrap', text: DASH });
  const nowSlide = el('span', { class: 'wrap', text: DASH });
  const nowViz = el('span', { class: 'wrap', text: DASH });
  const nowLook = el('span', { class: 'wrap', text: DASH });
  const heading = el('h3', { class: 'card__title', text: 'Now on air' });
  const skip = armButton({
    label: 'Skip track', armedLabel: 'Tap again to skip', icon: 'skip', variant: 'secondary', block: true, size: 'lg',
    onConfirm: () => skipTrack(name),
  });
  const nowCard = el('section', { class: 'card' }, [
    el('header', { class: 'card__head' }, [el('div', { class: 'card__titles' }, [heading])]),
    el('dl', { class: 'facts' }, [
      fact('Track', nowTrack), fact('Next', nowNext), fact('Slide', nowSlide), fact('Visualization', nowViz), fact('Look', nowLook),
    ]),
    skip,
    el('p', { class: 'note', text: 'Skipping is audio only. The video never notices.' }),
  ]);

  // --- health --------------------------------------------------------------
  const tiles = { speed: tile('speed'), fps: tile('fps'), bitrate: tile('bitrate'), cores: tile('cores') };
  const chips = el('div', { class: 'row' });
  const healthCard = card({
    title: 'Health',
    children: [el('div', { class: 'tiles' }, Object.values(tiles)), chips],
  });

  // --- power ---------------------------------------------------------------
  const bStart = btn({ label: 'Start', icon: 'play', variant: 'secondary', size: 'lg', mut: true, onClick: () => confirmLifecycle(name, 'start') });
  const bStop = btn({ label: 'Stop', icon: 'stop', variant: 'danger', size: 'lg', mut: true, onClick: () => confirmLifecycle(name, 'stop') });
  const bRestart = btn({ label: 'Restart', icon: 'restart', variant: 'secondary', size: 'lg', mut: true, onClick: () => confirmLifecycle(name, 'restart') });
  const powerNote = el('p', { class: 'note', text: '' });
  const powerCard = card({
    title: 'Power',
    subtitle: 'Each one asks you to press and hold before it does anything.',
    children: [el('div', { class: 'power' }, [bStart, bStop, bRestart]), powerNote],
  });

  // --- feeds ---------------------------------------------------------------
  const feeds = liveBox(() => {
    const reachable = deliveryOf(name).feeds.filter((f) => f.url);
    if (!reachable.length) return [];
    return [card({
      title: 'Watch elsewhere',
      subtitle: 'Open these in VLC on a device that can reach the host.',
      children: reachable.map((feed) => el('div', { class: 'feed' }, [
        el('div', { class: 'feed__head' }, [
          el('span', { class: 'feed__name', text: feed.label || feed.rendition }),
          feed.detail ? el('span', { class: 'muted num', text: feed.detail }) : null,
        ]),
        el('code', { class: 'feed__url', text: feed.url }),
        btn({ label: 'Copy address', icon: 'copy', variant: 'secondary', size: 'sm', onClick: () => copyText(feed.url, 'address copied') }),
      ])),
    })];
  });

  host.append(el('div', { class: 'stack' }, [problems.node, nowCard, healthCard, previewCard, powerCard, feeds.node]));

  function paint() {
    const ch = state.channels.get(name) || { name };
    const cfg = config(name);
    const running = channelIsRunning(name);
    const stopped = isStopped(name);

    heading.textContent = running ? 'Now on air' : 'Configured content';
    nowTrack.textContent = ch.current_track ? basename(ch.current_track) : DASH;
    nowNext.textContent = ch.next_track ? basename(ch.next_track) : DASH;
    nowSlide.textContent = ch.current_slide ? basename(ch.current_slide) : DASH;
    const off = !vizEnabled(name);
    nowViz.textContent = `${ch.visualization || DASH}${off ? '  (off \u2014 selected, not rendering)' : ''}`;
    nowLook.textContent = `preset ${cfg.preset || 'none'}  \u00B7  color ${cfg.color ? cfg.color.mode : DASH}`;
    skip.disabled = !running;

    tiles.speed.set(fmtNum(ch.speed, 3, 'x'), metricTone('speed', ch.speed));
    tiles.fps.set(fmtNum(ch.fps, 1));
    tiles.bitrate.set(typeof ch.bitrate_kbps === 'number' ? `${Math.round(ch.bitrate_kbps)}k` : DASH);
    tiles.cores.set(fmtNum(ch.cpu_cores, 2));
    chips.replaceChildren(
      chip(`rtmp ${ch.rtmp || DASH}`, metricTone('rtmp', ch.rtmp)),
      chip(`hls ${ch.hls || DASH}`, metricTone('hls', ch.hls)),
      chip(`audio ${ch.liquidsoap_buffer || DASH}`, metricTone('buffer', ch.liquidsoap_buffer)),
      chip(ch.encoder || DASH, 'idle'),
      chip(resolutionOf(name) || DASH, 'idle'),
      chip(running && ch.state === 'running' ? `up ${fmtUptime(ch.uptime_seconds)}` : 'not running', 'idle'),
    );

    bStart.disabled = !stopped;
    bStop.disabled = ch.state === 'stopped';
    bRestart.disabled = ch.state === 'stopped';
    powerNote.textContent = stopped
      ? 'The channel is stopped. Starting it publishes to its delivery targets.'
      : 'The channel is running. Stop and restart interrupt the broadcast.';
    startPreview.disabled = !running;
    if (!running && preview.channel) preview.stop();
  }

  paint();
  return {
    update(topics) {
      if (topics.has('status') || topics.has('detail') || topics.has('lock') || topics.has('viz') || topics.has('channels')) paint();
      if (topics.has('detail') || topics.has('viz')) {
        problems.refresh();
        feeds.refresh();
      }
      if (topics.has('status')) problems.refresh();
    },
    destroy() {
      preview.stop();
    },
  };
}
