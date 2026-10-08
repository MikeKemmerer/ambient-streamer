// Inline stroke icons. One path string per icon on a 24x24 grid; drawn with
// currentColor so tone and state come from the surrounding CSS.

const PATHS = {
  home: 'M3 10.5 12 3l9 7.5V21H3zM9 21v-6h6v6',
  channels: 'M4 6h16M4 12h16M4 18h16',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  music: 'M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  image: 'M3 5h18v14H3zM8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM3 17l5-5 4 4 3-3 6 6',
  sliders: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6',
  speaker: 'M11 5 6 9H2v6h4l5 4zM15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
  lock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4',
  unlock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 7.6-1.7',
  play: 'M7 4.5v15l12-7.5z',
  stop: 'M6 6h12v12H6z',
  restart: 'M21 12a9 9 0 1 1-3-6.7M21 3v6h-6',
  skip: 'M5 4.5v15L16 12zM19 5v14',
  back: 'M15 18l-6-6 6-6',
  forward: 'M9 18l6-6-6-6',
  down: 'M6 9l6 6 6-6',
  plus: 'M12 5v14M5 12h14',
  minus: 'M5 12h14',
  close: 'M18 6 6 18M6 6l12 12',
  up: 'M12 19V5M5 12l7-7 7 7',
  arrowdown: 'M12 5v14M19 12l-7 7-7-7',
  top: 'M5 4h14M12 20V8M6 14l6-6 6 6',
  bottom: 'M5 20h14M12 4v12M6 10l6 6 6-6',
  trash: 'M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6',
  check: 'M20 6 9 17l-5-5',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2',
  logs: 'M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8zM14 3v5h5M8 13h8M8 17h8',
  calendar: 'M4 5h16v16H4zM4 10h16M9 3v4M15 3v4',
  upload: 'M12 16V4M6 10l6-6 6 6M4 20h16',
  desktop: 'M3 4h18v12H3zM8 20h8M12 16v4',
  broadcast: 'M12 12h.01M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7M5.6 18.4a9 9 0 0 1 0-12.8M18.4 5.6a9 9 0 0 1 0 12.8',
  warning: 'M12 3 2 20h20zM12 10v4M12 17h.01',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  copy: 'M9 9h11v11H9zM5 15V5h10',
  undo: 'M3 7v6h6M3 13a9 9 0 1 0 3-7',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01',
  key: 'M15 8a4 4 0 1 1-3.5 6L3 14.5V18h3v-2h2v-2h2l1.5-1.5',
  server: 'M3 4h18v6H3zM3 14h18v6H3zM7 7h.01M7 17h.01',
  palette: 'M12 3a9 9 0 1 0 0 18c1.5 0 2-1 1.5-2s0-2 1.5-2h2a3 3 0 0 0 3-3 9 9 0 0 0-8-11zM7.5 12h.01M9 8h.01M14 7h.01',
  wave: 'M2 12h2M6 8v8M10 4v16M14 7v10M18 9v6M22 12h-2',
};

const NS = 'http://www.w3.org/2000/svg';

/** An <svg> for `name`. Decorative unless the caller adds a label. */
export function icon(name, { size = 22, stroke = 2, filled = false } = {}) {
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('fill', filled ? 'currentColor' : 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', String(stroke));
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.classList.add('icon');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', PATHS[name] || PATHS.info);
  svg.append(path);
  return svg;
}
