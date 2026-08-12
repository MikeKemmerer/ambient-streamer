// SSE client for GET /api/events.
//
// The browser's EventSource cannot set an Authorization header and the token must
// never travel in a URL, so the stream is read with fetch() and the event-stream
// framing is parsed here. Events are advisory: on every (re)connect the caller
// re-reads GET /api/channels, because there is no event log to replay.

export class EventStream {
  constructor({ url = '/api/events', headers = () => ({}), onEvent, onState }) {
    this.url = url;
    this.headers = headers;
    this.onEvent = onEvent;
    this.onState = onState;
    this.controller = null;
    this.stopped = true;
    this.attempt = 0;
    this.timer = null;
    // stop() then start() can outrun the in-flight abort rejection; the loop that
    // owns the current generation is the only one allowed to keep running.
    this.generation = 0;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.generation += 1;
    this.#loop(this.generation);
  }

  stop() {
    this.stopped = true;
    this.generation += 1;
    clearTimeout(this.timer);
    if (this.controller) this.controller.abort();
    this.controller = null;
    this.onState('idle');
  }

  async #loop(gen) {
    while (!this.stopped && gen === this.generation) {
      this.onState(this.attempt === 0 ? 'connecting' : 'reconnecting');
      try {
        await this.#connect(gen);
        if (this.stopped || gen !== this.generation) return;
        this.onState('dropped');
      } catch (err) {
        if (this.stopped || gen !== this.generation) return;
        if (err && err.status === 401) {
          this.onState('unauthorized');
          this.stopped = true;
          return;
        }
        this.onState('dropped');
      }
      const delay = [1000, 2000, 5000, 10000, 20000][Math.min(this.attempt, 4)];
      this.attempt += 1;
      await new Promise((resolve) => {
        this.timer = setTimeout(resolve, delay);
      });
    }
  }

  async #connect(gen) {
    const controller = new AbortController();
    this.controller = controller;
    const res = await fetch(this.url, {
      headers: this.headers({ Accept: 'text/event-stream' }),
      cache: 'no-store',
      signal: controller.signal,
    });

    if (!res.ok) {
      const err = new Error(`event stream ${res.status}`);
      err.status = res.status;
      throw err;
    }
    if (!res.body) throw new Error('event stream has no body');
    if (gen !== this.generation) {
      controller.abort();
      return;
    }

    this.attempt = 0;
    this.onState('connected');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (gen !== this.generation) {
        controller.abort();
        return;
      }
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        this.#frame(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
      }
      if (buffer.length > 1_000_000) buffer = ''; // a stream with no frame boundary is not ours
    }
  }

  #frame(raw) {
    let name = 'message';
    const data = [];
    for (const line of raw.split('\n')) {
      if (!line || line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') name = value;
      else if (field === 'data') data.push(value);
    }
    if (!data.length) return;
    let payload = null;
    try {
      payload = JSON.parse(data.join('\n'));
    } catch {
      payload = { raw: data.join('\n') };
    }
    this.onEvent(name, payload);
  }
}
