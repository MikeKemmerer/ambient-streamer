// Route helpers shared by the router and the screens.

export function hashFor(name, tab = 'live', sub = '') {
  const base = `#/c/${encodeURIComponent(name)}`;
  if (tab === 'live' && !sub) return base;
  return sub ? `${base}/${tab}/${sub}` : `${base}/${tab}`;
}
