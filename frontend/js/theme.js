/* Colour themes.

   The palette lives entirely in CSS custom properties under [data-theme] in
   app.css; this module only decides which one is active and remembers it. The
   risk ramp is deliberately NOT part of a theme — a Very-High pixel has to stay
   the same red whichever skin is on, or the map stops meaning anything. */

/* White leads the list because it is the default and the one the platform is
   presented in; the darker skins stay available for a dim room or a bad projector. */
export const THEMES = [
  { id: 'white',  label: 'White',  dot: '#e9ece7' },
  { id: 'forest', label: 'Forest', dot: '#17685c' },
  { id: 'night',  label: 'Night',  dot: '#16305c' },
  { id: 'grey',   label: 'Grey',   dot: '#3a3f43' },
  { id: 'black',  label: 'Black',  dot: '#101010' },
];

const KEY = 'sikkim.theme';
/* A dark theme sinks the base map into its own ground so the map furniture stays
   legible. This opts out of that for people who want the tiles at full strength
   under a dark UI — meaningless on the light theme, which never sinks them. */
const BASEMAP_KEY = 'sikkim.lightBasemap';
/* Light by default: the review asked for a light presentation, and the pale
   base maps are the ones the platform now ships with. */
const DEFAULT = 'white';

export function lightBasemap() {
  try { return localStorage.getItem(BASEMAP_KEY) === '1'; } catch { return false; }
}

export function setLightBasemap(on) {
  try { localStorage.setItem(BASEMAP_KEY, on ? '1' : '0'); } catch { /* private mode */ }
  syncGround();
  return on;
}

/* Is the map's ground light? True on the light theme, and on any theme where the
   viewer has asked for full-strength tiles. Everything drawn ON the map — the
   rail, the readout, the legend, the markers — keys off this rather than off the
   theme, so a dark UI with a light base map still gets dark type on the map. */
export function groundIsLight() {
  return currentTheme() === 'white' || lightBasemap();
}

/* Mirrored onto <html> so CSS can reach it. */
function syncGround() {
  document.documentElement.toggleAttribute('data-light-ground', groundIsLight());
}

export function currentTheme() {
  try {
    const v = localStorage.getItem(KEY);
    if (v && THEMES.some(t => t.id === v)) return v;
  } catch { /* private mode */ }
  return DEFAULT;
}

export function applyTheme(id) {
  const theme = THEMES.some(t => t.id === id) ? id : DEFAULT;
  document.documentElement.setAttribute('data-theme', theme);
  // Light themes need the browser's own form controls and scrollbars to flip too.
  document.documentElement.style.colorScheme = theme === 'white' ? 'light' : 'dark';
  try { localStorage.setItem(KEY, theme); } catch { /* private mode */ }
  syncGround();
  return theme;
}

/* A dropdown in the nav. onChange fires after the attribute is set, so callers
   can re-read anything that depends on the new palette. */
export function mountThemePicker({ button, menu }, onChange) {
  const paint = () => {
    const active = currentTheme();
    const rows = THEMES.map(t => `
      <button class="theme-item ${t.id === active ? 'is-active' : ''}" data-theme="${t.id}"
              role="menuitemradio" aria-checked="${t.id === active}">
        <i style="background:${t.dot}"></i><span>${t.label}</span>
      </button>`).join('');
    // Only offered where it means something: the light theme never sinks the tiles.
    const on = lightBasemap();
    const toggle = active === 'white' ? '' : `
      <div class="theme-sep"></div>
      <button class="theme-toggle ${on ? 'is-on' : ''}" data-toggle="light-basemap"
              role="menuitemcheckbox" aria-checked="${on}">
        <span>Light base map</span><i class="switch"></i>
      </button>`;
    menu.innerHTML = rows + toggle;
  };
  paint();

  const close = () => {
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    button.classList.remove('is-on');
  };
  const open = () => {
    paint();
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    button.classList.add('is-on');
  };

  button.addEventListener('click', e => {
    e.stopPropagation();
    menu.hidden ? open() : close();
  });

  menu.addEventListener('click', e => {
    // The base-map switch leaves the menu open: it changes the map behind the menu,
    // and closing it would hide the thing the click was about.
    const toggle = e.target.closest('.theme-toggle');
    if (toggle) {
      setLightBasemap(!lightBasemap());
      paint();
      onChange(currentTheme());
      return;
    }
    const item = e.target.closest('.theme-item');
    if (!item) return;
    onChange(applyTheme(item.dataset.theme));
    close();
  });

  // Click-away and Escape, the two things a menu has to honour.
  document.addEventListener('click', e => {
    if (!menu.hidden && !e.target.closest('.theme-menu')) close();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !menu.hidden) close();
  });
}
