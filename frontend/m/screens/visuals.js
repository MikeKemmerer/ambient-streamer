// Visuals: the slideshow, the visualizer, colour and presets.

import {
  applyColor, applyOpacity, applyParameters, applyPreset, confirmDiscard, confirmVizPower, confirmVizVisible, saveSlides,
  switchVisualization,
} from '../data/actions.js';
import {
  chanData, channelIsRunning, config, isListDirty, listDiff, listRevert, listUndo, loadSelection, numberOr, setSlidesForm,
  slidesFormDirty, slidesFormOf, state, vizEnabled,
} from '../data/store.js';
import {
  btn, callout, card, chip, el, field, segmented, selectInput, spinner, stepper, switchRow, textInput, toast,
} from '../lib/ui.js';
import { liveBox, mediaPicker, orderedList, saveBar, slider, uploadPanel } from './common.js';

const HEX = /^#[0-9a-fA-F]{6}$/;

function slidesLabel(name) {
  const d = chanData(name);
  const diff = listDiff(d.slides);
  const parts = [];
  if (diff.added) parts.push(`${diff.added} added`);
  if (diff.removed) parts.push(`${diff.removed} removed`);
  if (diff.reordered) parts.push('reordered');
  if (slidesFormDirty(name)) parts.push('settings changed');
  return parts.join(' \u00B7 ') || 'Unsaved changes';
}

// --------------------------------------------------------------------------
// Colour form: held as a draft until applied
// --------------------------------------------------------------------------

function savedColor(name) {
  const color = config(name).color || {};
  const manual = color.manual || {};
  return {
    mode: color.mode || 'automatic',
    accent: HEX.test(manual.accent || '') ? manual.accent.toUpperCase() : '#4FC3F7',
    tint: HEX.test(manual.tint || '') ? manual.tint.toUpperCase() : '#101820',
    transition: numberOr(color.transition_seconds, 2),
  };
}

function colorForm(name) {
  return chanData(name).color || savedColor(name);
}

function colorDirty(name) {
  const d = chanData(name);
  return Boolean(d.color) && JSON.stringify(d.color) !== JSON.stringify(savedColor(name));
}

function colorPicker(label, value, onChange) {
  const swatch = el('input', { type: 'color', class: 'input colorpick', 'data-mut': '', 'aria-label': `${label} colour` });
  swatch.value = HEX.test(value) ? value : '#000000';
  const hex = textInput({ value: value.toUpperCase(), mono: true, maxlength: '7', 'aria-label': `${label} hex` });
  swatch.addEventListener('change', () => onChange(swatch.value.toUpperCase()));
  hex.addEventListener('change', () => {
    const v = hex.value.trim();
    if (HEX.test(v)) onChange(v.toUpperCase());
    else hex.value = value.toUpperCase();
  });
  return field(label, el('div', { class: 'colorrow' }, [swatch, hex]));
}

// --------------------------------------------------------------------------
// Plugin tuner
// --------------------------------------------------------------------------

function tuner(name, plugin, refresh) {
  const specs = Array.isArray(plugin.parameters) ? plugin.parameters : [];
  if (!specs.length) return null;
  const d = chanData(name);
  const saved = ((config(name).visualization || {}).parameters || {})[plugin.name] || {};
  const draft = d.params[plugin.name] || {};
  const valueOf = (spec) => (spec.name in draft ? draft[spec.name] : spec.name in saved ? saved[spec.name] : spec.default);
  const dirty = specs.some((spec) => {
    const current = spec.name in saved ? saved[spec.name] : spec.default;
    return spec.name in draft && String(draft[spec.name]) !== String(current);
  });
  const stage = (spec, raw) => {
    d.params[plugin.name] = { ...(d.params[plugin.name] || {}), [spec.name]: raw };
  };

  const rows = specs.map((spec) => {
    const value = valueOf(spec);
    let control;
    if (spec.type === 'bool') {
      control = switchRow({
        label: spec.label || spec.name, hint: spec.description, checked: Boolean(value),
        onChange: (on) => { stage(spec, on); refresh(); },
      });
      return control;
    }
    if (spec.type === 'enum') {
      control = selectInput({
        options: (spec.choices || []).map((c) => ({ value: String(c.value), label: c.label || String(c.value) })),
        value: String(value),
        onChange: (v) => { stage(spec, v); refresh(); },
      });
    } else {
      const decimals = String(spec.step || 1).includes('.') ? String(spec.step).split('.')[1].length : 0;
      control = slider({
        min: Number(spec.min), max: Number(spec.max), step: Number(spec.step) || 1, value: Number(value), decimals,
        ariaLabel: spec.label || spec.name,
        onChange: (v) => { stage(spec, v); refresh(); },
      });
    }
    return field(spec.label || spec.name, control, spec.description);
  });

  const open = dirty;
  return el('details', { class: 'tuner', open }, [
    el('summary', { class: 'tuner__sum', text: `Tune \u00B7 ${specs.length} setting${specs.length === 1 ? '' : 's'}` }),
    el('div', { class: 'stack' }, [
      ...rows,
      dirty
        ? el('p', {
          class: 'note',
          text: channelIsRunning(name) && (config(name).visualization || {}).active === plugin.name
            ? 'Apply restarts only the active visualizer child; the compositor and stream continue.'
            : 'Apply saves these settings for the next time this visualization starts.',
        })
        : null,
      el('div', { class: 'row' }, [
        btn({ label: 'Reset', variant: 'quiet', size: 'sm', mut: true, disabled: !dirty, onClick: () => { delete d.params[plugin.name]; refresh(); } }),
        el('span', { class: 'spacer' }),
        btn({ label: 'Apply settings', icon: 'check', variant: 'primary', size: 'sm', mut: true, disabled: !dirty, onClick: () => applyParameters(name, plugin, specs) }),
      ]),
    ]),
  ]);
}

// --------------------------------------------------------------------------
// Screen
// --------------------------------------------------------------------------

export function mountVisuals(host, ctx) {
  const name = ctx.name;
  const d = chanData(name);
  const ui = { sub: ['slides', 'images', 'viz', 'color'].includes(ctx.sub) ? ctx.sub : 'slides', selected: null };

  const seg = segmented({
    ariaLabel: 'visual sections',
    options: [
      { value: 'slides', label: 'Slides' },
      { value: 'images', label: 'Add images' },
      { value: 'viz', label: 'Visualizer' },
      { value: 'color', label: 'Color' },
    ],
    value: ui.sub,
    onChange: (value) => { ui.sub = value; ctx.replaceSub(value); paintSub(); },
  });
  const body = el('div', { class: 'stack' });
  const bar = saveBar({
    saveLabel: 'Save slides',
    onSave: () => saveSlides(name),
    onUndo: () => listUndo(name, 'slides'),
    onDiscard: async () => {
      const diff = listDiff(d.slides);
      const count = diff.added + diff.removed + (diff.reordered ? 1 : 0) + (slidesFormDirty(name) ? 1 : 0);
      if (await confirmDiscard('your slide edits', count)) {
        listRevert(name, 'slides');
        d.slidesForm = null;
        paintSub();
      }
    },
  });
  host.append(el('div', { class: 'segbar' }, [seg]), body, bar);

  // --- slides --------------------------------------------------------------
  const editor = orderedList({ name, kind: 'slides', ui, noun: 'image' });
  const listCard = liveBox(() => [card({
    title: `Slides \u00B7 ${d.slides.draft.length}`,
    subtitle: 'Tap an image to move or remove it. The slideshow rescans between slides \u2014 no encoder restart.',
    actions: isListDirty(d.slides) ? chip('unsaved', 'warn') : null,
    children: [!d.selectionLoaded ? spinner('Loading the slides\u2026') : editor.node],
  })]);
  const settings = liveBox(() => {
    const form = slidesFormOf(name);
    return [card({
      title: 'Timing',
      children: [
        field('Order', segmented({
          ariaLabel: 'slide order', value: form.order,
          options: [{ value: 'sequential', label: 'Sequential' }, { value: 'shuffle', label: 'Shuffle' }],
          onChange: (v) => setSlidesForm(name, { order: v }),
        })),
        field('Hold each slide', stepper({
          value: form.hold, min: 1, max: 600, step: 1, unit: 's', ariaLabel: 'hold seconds',
          onChange: (v) => setSlidesForm(name, { hold: v }),
        })),
        field('Crossfade between slides', stepper({
          value: form.fade, min: 0, max: 60, step: 0.5, decimals: 1, unit: 's', ariaLabel: 'fade seconds',
          onChange: (v) => setSlidesForm(name, { fade: v }),
        })),
      ],
    })];
  });

  // --- library -------------------------------------------------------------
  const picker = mediaPicker({ name, kind: 'images', ui, noun: 'image' });
  const upload = uploadPanel({ kind: 'images', getChannel: () => name });
  const library = card({
    title: 'Library',
    subtitle: 'Images you add go to the end of the slide list. Nothing changes until you save.',
    children: [picker.node],
  });

  // --- visualizer ----------------------------------------------------------
  const viz = liveBox(() => {
    const cfg = config(name);
    const vcfg = cfg.visualization || {};
    const running = channelIsRunning(name);
    const enabled = vizEnabled(name);
    const pending = state.vizPending.get(name);
    const active = (state.channels.get(name) || {}).visualization || vcfg.active || '';
    const visible = vcfg.visible !== false;
    const savedOpacity = Math.round(Math.max(0, Math.min(1, Number(vcfg.opacity ?? 0.65))) * 100);
    const opacity = d.opacity ?? savedOpacity;

    const power = switchRow({
      label: 'Visualization on',
      hint: running
        ? 'Starts or stops only the visualizer child. The stream keeps running on a transparent fallback.'
        : 'Saved now and applied the next time the channel starts.',
      checked: enabled,
      onChange: (on) => {
        power.input.checked = !on;
        confirmVizPower(name, on);
      },
    });
    const standby = enabled
      ? switchRow({
        label: running ? 'Showing on air' : 'Show on next start',
        hint: visible ? '' : (running ? 'On standby: the visualizer is still rendering and using CPU.' : 'Hidden when the channel next starts.'),
        checked: visible,
        onChange: (on) => {
          standby.input.checked = !on;
          confirmVizVisible(name, on);
        },
      })
      : null;

    const dirty = opacity !== savedOpacity;
    const applyOp = btn({
      label: 'Apply opacity', icon: 'check', variant: 'primary', size: 'sm', mut: true, disabled: !dirty,
      // Read at tap time: the slider updates the draft without re-rendering this panel.
      onClick: () => applyOpacity(name, d.opacity ?? savedOpacity),
    });
    const op = slider({
      min: 0, max: 100, step: 1, value: opacity, unit: '%', ariaLabel: 'visualization opacity',
      onInput: (v) => {
        d.opacity = v;
        applyOp.disabled = v === savedOpacity;
      },
      onChange: (v) => { d.opacity = v; applyOp.disabled = v === savedOpacity; },
    });

    const plugins = state.plugins.map((plugin) => {
      const isActive = plugin.name === active;
      const missing = plugin.available === false;
      const cost = plugin.cost && typeof plugin.cost.cores_720p30 === 'number'
        ? `${plugin.cost.cores_720p30.toFixed(2)} cores @720p30` : '';
      const pluginPending = pending && pending.plugin === plugin.name;
      const tag = missing ? chip('not installed', 'bad')
        : pluginPending ? chip(pending.state, 'info')
          : isActive ? chip(enabled && running ? 'active' : 'selected', enabled && running ? 'ok' : 'info')
            : chip('installed', 'idle');
      return el('li', { class: 'plugin', dataset: { active: String(isActive), missing: String(missing) } }, [
        el('div', { class: 'plugin__head' }, [
          el('div', { class: 'plugin__titles' }, [
            el('span', { class: 'plugin__name', text: plugin.display_name || plugin.name }),
            el('span', { class: 'plugin__id mono', text: plugin.name }),
          ]),
          tag,
        ]),
        plugin.description ? el('p', { class: 'note', text: plugin.description }) : null,
        cost ? el('p', { class: 'note mono', text: cost }) : null,
        isActive ? null : btn({
          label: 'Switch to this', variant: 'secondary', size: 'sm', mut: true, disabled: missing,
          onClick: () => switchVisualization(name, plugin.name),
        }),
        tuner(name, plugin, () => viz.refresh()),
      ]);
    });

    return [
      card({
        title: 'Visualization',
        children: [
          power,
          standby,
          pending ? callout(`${pending.label} \u00B7 generation ${pending.generation} \u00B7 ${pending.state}`, 'info', 'info') : null,
          enabled ? field('Opacity', op, running ? 'Lands in one frame.' : 'Applies on the next start.') : null,
          enabled ? el('div', { class: 'row' }, [el('span', { class: 'spacer' }), applyOp]) : null,
        ],
      }),
      card({
        title: 'Plugins',
        subtitle: running
          ? 'Switching replaces only the visualizer child; the YouTube ingest session stays up.'
          : 'Choose a plugin for the next start. Saving does not start a stopped channel.',
        children: [state.plugins.length ? el('ul', { class: 'plugins' }, plugins) : el('p', { class: 'note', text: 'No plugins reported.' })],
      }),
    ];
  }, { cls: 'stack' });

  // --- colour and preset ---------------------------------------------------
  const look = liveBox(() => {
    const form = colorForm(name);
    const dirty = colorDirty(name);
    const set = (patch) => { d.color = { ...colorForm(name), ...patch }; look.refresh(); };
    const presetSel = selectInput({
      options: [{ value: '', label: '\u2014 none \u2014' }, ...state.presets.map((p) => ({ value: p.name, label: p.display_name || p.name }))],
      value: config(name).preset || '',
    });
    const desc = el('p', { class: 'note', text: '' });
    const applyBtn = btn({ label: 'Apply preset', icon: 'check', variant: 'secondary', mut: true, block: true, onClick: () => applyPreset(name, presetSel.value) });
    const showDesc = () => {
      const found = state.presets.find((p) => p.name === presetSel.value);
      desc.textContent = found ? found.description || '' : '';
      applyBtn.disabled = !presetSel.value;
    };
    presetSel.addEventListener('change', showDesc);
    showDesc();

    return [
      card({
        title: 'Color',
        subtitle: form.mode === 'automatic'
          ? 'Automatic derives colors per slide from the image; the manual values are ignored.'
          : 'Manual colors apply to every slide.',
        children: [
          segmented({
            ariaLabel: 'color mode', value: form.mode,
            options: [{ value: 'automatic', label: 'Automatic' }, { value: 'manual', label: 'Manual' }],
            onChange: (v) => set({ mode: v }),
          }),
          colorPicker('Accent', form.accent, (v) => set({ accent: v })),
          colorPicker('Tint', form.tint, (v) => set({ tint: v })),
          field('Transition', stepper({
            value: form.transition, min: 0, max: 60, step: 0.5, decimals: 1, unit: 's', ariaLabel: 'transition seconds',
            onChange: (v) => set({ transition: v }),
          })),
          el('div', { class: 'row' }, [
            dirty ? btn({ label: 'Reset', variant: 'quiet', size: 'sm', mut: true, onClick: () => { d.color = null; look.refresh(); } }) : null,
            el('span', { class: 'spacer' }),
            // Always tappable and read at tap time: a hex field that is still focused
            // defers this panel's rebuild, so what was captured at render may be stale.
            btn({
              label: 'Apply color', icon: 'check', variant: 'primary', mut: true,
              onClick: () => {
                if (!colorDirty(name)) {
                  toast('info', 'nothing to apply', 'Change a color first.');
                  return;
                }
                applyColor(name, colorForm(name));
              },
            }),
          ]),
        ],
      }),
      card({
        title: 'Preset',
        subtitle: 'A preset never restarts the stream.',
        children: [field('Preset', presetSel), desc, applyBtn],
      }),
    ];
  }, { cls: 'stack' });

  function paintSub() {
    seg.set(ui.sub);
    if (ui.sub === 'slides') body.replaceChildren(listCard.node, settings.node);
    else if (ui.sub === 'images') body.replaceChildren(library, upload.node);
    else if (ui.sub === 'viz') body.replaceChildren(viz.node);
    else body.replaceChildren(look.node);
    paintBar();
  }

  function paintBar() {
    const dirty = (isListDirty(d.slides) || slidesFormDirty(name)) && (ui.sub === 'slides' || ui.sub === 'images');
    bar.set({ dirty, label: slidesLabel(name), canUndo: d.slides.undo.length > 0 });
    const first = seg.querySelector('.seg__btn');
    const flag = isListDirty(d.slides) || slidesFormDirty(name);
    if (first) {
      const had = first.querySelector('.seg__badge');
      if (flag && !had) first.append(el('span', { class: 'seg__badge', dataset: { tone: 'warn' }, text: 'unsaved' }));
      if (!flag && had) had.remove();
    }
  }

  paintSub();
  loadSelection(name);

  return {
    update(topics) {
      if (topics.has('drafts') || topics.has('lists')) {
        listCard.refresh();
        editor.refresh();
        settings.refresh();
        picker.refresh();
        paintBar();
      }
      if (topics.has('media')) picker.refresh();
      if (topics.has('uploads')) upload.refresh();
      if (topics.has('detail') || topics.has('viz') || topics.has('plugins') || topics.has('presets')) {
        viz.refresh();
        look.refresh();
        settings.refresh();
      }
      if (topics.has('status')) {
        const ch = state.channels.get(name) || {};
        const sig = `${ch.state}|${ch.visualization}|${ch.visualization_enabled}`;
        if (sig !== ui.vizSig) {
          ui.vizSig = sig;
          viz.refresh();
        }
      }
    },
  };
}
