// Audio: the playlist, putting a track on air, and the library.

import { confirmDiscard, playTrack, savePlaylist } from '../data/actions.js';
import {
  audioFormDirty, audioFormOf, chanData, channelState, isListDirty, listDiff, listRevert, listUndo, loadSelection, setAudioForm, state,
} from '../data/store.js';
import { armButton, btn, card, chip, el, empty, field, segmented, spinner, stepper, switchRow, textInput } from '../lib/ui.js';
import { liveBox, mediaPicker, orderedList, pathName, saveBar, uploadPanel } from './common.js';

const CHUNK = 40;

function changeLabel(name) {
  const d = chanData(name);
  const diff = listDiff(d.playlist);
  const parts = [];
  if (diff.added) parts.push(`${diff.added} added`);
  if (diff.removed) parts.push(`${diff.removed} removed`);
  if (diff.reordered) parts.push('reordered');
  if (audioFormDirty(name)) parts.push('settings changed');
  return parts.join(' \u00B7 ') || 'Unsaved changes';
}

export function mountAudio(host, ctx) {
  const name = ctx.name;
  const d = chanData(name);
  const ui = { sub: ['playlist', 'playnow', 'library'].includes(ctx.sub) ? ctx.sub : 'playlist', selected: null };
  let playSig = '';

  const seg = segmented({
    ariaLabel: 'audio sections',
    options: [
      { value: 'playlist', label: 'Playlist', badge: '' },
      { value: 'playnow', label: 'Play now' },
      { value: 'library', label: 'Add tracks' },
    ],
    value: ui.sub,
    onChange: (value) => { ui.sub = value; ctx.replaceSub(value); paintSub(); },
  });

  const body = el('div', { class: 'stack' });
  const bar = saveBar({
    saveLabel: 'Save playlist',
    onSave: () => savePlaylist(name),
    onUndo: () => listUndo(name, 'audio'),
    onDiscard: async () => {
      const diff = listDiff(d.playlist);
      const count = diff.added + diff.removed + (diff.reordered ? 1 : 0) + (audioFormDirty(name) ? 1 : 0);
      if (await confirmDiscard('your playlist edits', count)) {
        listRevert(name, 'audio');
        d.audioForm = null;
        paintSub();
        paintBar();
      }
    },
  });

  host.append(el('div', { class: 'segbar' }, [seg]), body, bar);

  // --- playlist ------------------------------------------------------------
  const editor = orderedList({ name, kind: 'audio', ui, noun: 'track' });

  const settings = liveBox(() => {
    const form = audioFormOf(name);
    return [card({
      title: 'Playback',
      children: [
        switchRow({
          label: 'Shuffle', hint: 'Play the list in random order.', checked: form.shuffle,
          onChange: (on) => setAudioForm(name, { shuffle: on }),
        }),
        field('Crossfade', stepper({
          value: form.crossfade, min: 0, max: 30, step: 0.5, decimals: 1, unit: 's', ariaLabel: 'crossfade seconds',
          onChange: (v) => setAudioForm(name, { crossfade: v }),
        }), 'Overlap between tracks.'),
      ],
    })];
  });

  const listCard = liveBox(() => [card({
    title: `Playlist \u00B7 ${d.playlist.draft.length}`,
    subtitle: 'Order is the play order. Tap a track to move or remove it.',
    actions: isListDirty(d.playlist) ? chip('unsaved', 'warn') : null,
    children: [!d.selectionLoaded ? spinner('Loading the playlist\u2026') : editor.node],
  })]);

  // --- play now ------------------------------------------------------------
  const nowFilter = textInput({ type: 'search', placeholder: 'search the live list', mut: false });
  nowFilter.addEventListener('input', () => { ui.nowFilter = nowFilter.value; ui.nowShown = CHUNK; nowBox.refresh(); });
  const nowBox = liveBox(() => {
    const ch = state.channels.get(name) || {};
    const running = ch.state === 'running';
    const entries = d.playlist.onair;
    const needle = (ui.nowFilter || '').trim().toLowerCase();
    const shown = ui.nowShown || CHUNK;
    if (!d.selectionLoaded) return spinner('Loading\u2026');
    if (!entries.length) return empty('The live list is empty', 'Nothing resolves to a playable file yet.');
    const matches = entries.map((entry, index) => ({ entry, index }))
      .filter(({ entry }) => !needle || entry.label.toLowerCase().includes(needle));
    if (!matches.length) return empty('No tracks match');
    const rows = matches.slice(0, shown).map(({ entry, index }) => {
      const playing = entry.label === ch.current_track;
      return el('li', { class: 'prow', dataset: { playing: String(playing) } }, [
        el('span', { class: 'orow__idx num', text: playing ? '\u25B6' : String(index + 1) }),
        pathName(entry.label),
        playing
          ? chip('on air', 'ok', 'chip--plain')
          : armButton({
            label: 'Play', armedLabel: 'Tap again', icon: 'play', size: 'sm', variant: 'secondary', disabled: !running,
            onConfirm: () => playTrack(name, entry.path),
          }),
      ]);
    });
    if (matches.length > shown) {
      rows.push(el('li', {}, [btn({
        label: `Show more (${matches.length - shown} left)`, variant: 'quiet', block: true,
        onClick: () => { ui.nowShown = shown + CHUNK; nowBox.refresh(); },
      })]));
    }
    return [
      running ? null : el('p', { class: 'note', text: 'Start the channel to play a track from here.' }),
      el('ul', { class: 'plist' }, rows),
    ];
  });
  const nowCard = card({
    title: 'Play now',
    subtitle: 'Interrupts what is playing. Audio only \u2014 the video never restarts.',
    children: [nowFilter, nowBox.node],
  });

  // --- library -------------------------------------------------------------
  const picker = mediaPicker({ name, kind: 'audio', ui, noun: 'track' });
  const upload = uploadPanel({ kind: 'audio', getChannel: () => name });
  const libraryCard = card({
    title: 'Library',
    subtitle: 'Tracks you add go to the end of the playlist. Nothing changes until you save.',
    children: [picker.node],
  });

  function paintSub() {
    seg.set(ui.sub);
    if (ui.sub === 'playlist') body.replaceChildren(listCard.node, settings.node);
    else if (ui.sub === 'playnow') body.replaceChildren(nowCard);
    else body.replaceChildren(libraryCard, upload.node);
    paintBar();
  }

  function paintBar() {
    const dirty = isListDirty(d.playlist) || audioFormDirty(name);
    bar.set({ dirty, label: changeLabel(name), canUndo: d.playlist.undo.length > 0 });
    const badge = seg.querySelector('.seg__btn');
    if (badge) {
      const had = badge.querySelector('.seg__badge');
      if (dirty && !had) badge.append(el('span', { class: 'seg__badge', dataset: { tone: 'warn' }, text: 'unsaved' }));
      if (!dirty && had) had.remove();
    }
  }

  paintSub();
  loadSelection(name);

  return {
    update(topics) {
      if (topics.has('drafts') || topics.has('lists') || topics.has('detail')) {
        listCard.refresh();
        editor.refresh();
        settings.refresh();
        picker.refresh();
        paintBar();
      }
      if (topics.has('media')) picker.refresh();
      if (topics.has('lists')) nowBox.refresh();
      if (topics.has('uploads')) upload.refresh();
      if (topics.has('status')) {
        const ch = state.channels.get(name) || {};
        const sig = `${ch.current_track}|${channelState(name)}`;
        if (sig !== playSig) {
          playSig = sig;
          nowBox.refresh();
        }
      }
    },
  };
}
