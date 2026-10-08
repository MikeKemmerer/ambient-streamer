// Sounds: sound effects the stream plays on demand.

import { api } from '../../api.js';
import { sendSound, stopSounds } from '../data/actions.js';
import { chanData, channelState, guard, loadSoundboard, state } from '../data/store.js';
import { card, chip, el, empty, holdButton, iconBtn, spinner, textInput, toast } from '../lib/ui.js';
import { icon } from '../lib/icons.js';
import { liveBox, uploadPanel } from './common.js';

export function mountSounds(host, ctx) {
  const name = ctx.name;
  const sb = chanData(name).soundboard;
  const ui = { filter: '' };

  // --- local preview: plays on this phone only -----------------------------
  let audio = null;
  let url = null;
  let previewClip = null;
  let token = 0;

  function stopPreview() {
    token += 1;
    if (audio) audio.pause();
    if (url) URL.revokeObjectURL(url);
    audio = null;
    url = null;
    previewClip = null;
    pads.refresh();
  }

  async function startPreview(clip) {
    if (previewClip === clip.container_path && audio) {
      stopPreview();
      return;
    }
    stopPreview();
    const mine = token;
    const blob = await guard(`preview ${clip.name}`, () => api.previewSound(name, clip.container_path));
    if (blob === undefined || mine !== token) return;
    previewClip = clip.container_path;
    url = URL.createObjectURL(blob);
    audio = new Audio(url);
    audio.addEventListener('ended', stopPreview, { once: true });
    pads.refresh();
    try {
      await audio.play();
    } catch (err) {
      stopPreview();
      toast('bad', 'preview', String((err && err.message) || err));
    }
  }

  // --- pads ----------------------------------------------------------------
  const filter = textInput({ type: 'search', placeholder: 'search sounds', mut: false });
  filter.setAttribute('aria-label', 'search sounds');
  filter.addEventListener('input', () => { ui.filter = filter.value; pads.refresh(); });

  const pads = liveBox(() => {
    const needle = ui.filter.trim().toLowerCase();
    const clips = sb.clips.filter((c) => !needle || `${c.name} ${c.path}`.toLowerCase().includes(needle));
    if (!state.data.get(name).detailLoaded && !sb.clips.length) return spinner('Loading sounds\u2026');
    if (!clips.length) {
      return empty(sb.clips.length ? 'No sounds match' : 'No sound effects yet', sb.clips.length ? '' : 'Upload some below.');
    }
    return el('div', { class: 'pads' }, clips.map((clip) => {
      const selected = clip.container_path === sb.selected;
      const playing = previewClip === clip.container_path;
      return el('div', { class: 'pad', dataset: { selected: String(selected) } }, [
        el('button', {
          type: 'button', class: 'pad__main', 'aria-pressed': String(selected),
          onclick: () => { sb.selected = selected ? null : clip.container_path; pads.refresh(); sendbar.refresh(); },
        }, [
          el('span', { class: 'pad__name', text: clip.name }),
          el('span', { class: 'pad__origin', text: clip.origin }),
        ]),
        el('button', {
          type: 'button', class: 'pad__preview', 'aria-label': playing ? `stop preview of ${clip.name}` : `preview ${clip.name} on this phone`,
          onclick: () => startPreview(clip),
        }, [icon(playing ? 'stop' : 'play', { size: 20 }), el('span', { text: playing ? 'Stop' : 'Preview' })]),
      ]);
    }));
  });

  // --- send bar ------------------------------------------------------------
  const sendbar = liveBox(() => {
    const running = channelState(name) === 'running';
    const clip = sb.clips.find((c) => c.container_path === sb.selected);
    const send = holdButton({
      label: clip ? `Hold to send \u201C${clip.name}\u201D` : 'Select a sound',
      holdLabel: 'Keep holding\u2026', ms: 900, tone: 'warn', icon: 'speaker',
      onConfirm: async () => {
        await sendSound(name, clip);
        setTimeout(sendbar.refresh, 500);
      },
    });
    if (!running || !clip) {
      send.setAttribute('data-blocked', '');
      send.setAttribute('aria-disabled', 'true');
    }
    const stop = iconBtn({ name: 'stop', label: 'Stop sound effects', variant: 'danger', onClick: () => stopSounds(name), disabled: !running });
    stop.classList.add('keep-live');
    return [
      el('div', { class: 'sendbar__info' }, [
        running
          ? el('span', { class: 'note', text: 'Sent sounds play on the live stream. Hold the button to send.' })
          : chip('channel not running', 'warn'),
      ]),
      el('div', { class: 'sendbar__row' }, [send, stop]),
    ];
  }, { cls: 'sendbar' });

  const upload = uploadPanel({ kind: 'soundboard', getChannel: () => name, title: 'Upload sounds' });

  host.append(el('div', { class: 'stack' }, [
    card({
      title: 'Soundboard',
      subtitle: 'Tap a pad to select it. Preview plays on this phone only.',
      children: [filter, pads.node],
    }),
    upload.node,
  ]), sendbar.node);

  loadSoundboard(name);

  return {
    update(topics) {
      if (topics.has('soundboard') || topics.has('detail')) {
        pads.refresh();
        sendbar.refresh();
      }
      if (topics.has('status')) {
        const running = channelState(name) === 'running';
        if (running !== ui.running) {
          ui.running = running;
          sendbar.refresh();
        }
      }
      if (topics.has('uploads')) upload.refresh();
    },
    destroy() {
      stopPreview();
    },
  };
}
