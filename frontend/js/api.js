/* Thin API client. Every network call in the app goes through here. */
const BASE = '/api';

/* Set once from /api/meta at boot; see tileUrl below. */
let DATA_VERSION = '';
export function setDataVersion(v) { DATA_VERSION = v || ''; }

async function get(path, params) {
  const url = new URL(BASE + path, location.origin);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url.pathname}`);
  return res.json();
}

export const api = {
  meta:      ()            => get('/meta'),
  stats:     ()            => get('/stats'),
  legend:    (id)          => get(`/layers/${id}/legend`),
  point:     (lon, lat)    => get('/point', { lon, lat }),
  hotspots:  (p)           => get('/hotspots', p),
  hotspot:   (rank)        => get(`/hotspots/${rank}`),
  inventory: (p)           => get('/inventory', p),
  facets:    ()            => get('/inventory/facets'),
  places:    ()            => get('/places'),
  search:    (q)           => get('/search', { q }),
  alerts:    (p)           => get('/alerts', p),
  liveRain:  ()            => get('/live-rain'),
  bulletin:  ()            => get('/bulletin'),
  corridor:  ()            => get('/corridor'),
  shelters:  (p)           => get('/shelters', p),
  reports:   ()            => get('/reports'),
  async report(body) {
    const res = await fetch(BASE + '/report', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  },
  /* Tiles are cached for a day. `v` is the pipeline's own build token, so a
     `make data` produces new URLs and clients stop drawing stale imagery. */
  tileUrl(id, params = {}) {
    const q = new URLSearchParams();
    if (DATA_VERSION) q.set('v', DATA_VERSION);
    for (const [k, val] of Object.entries(params)) {
      if (val !== undefined && val !== null && val !== '') q.set(k, val);
    }
    const qs = q.toString();
    return `${location.origin}${BASE}/tiles/${id}/{z}/{x}/{y}.png${qs ? `?${qs}` : ''}`;
  },
};

/* ---- formatting helpers used across the UI ---- */
export const fmt = {
  num(v, d = 0) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return Number(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });
  },
  pct(v, d = 1) {
    if (v === null || v === undefined) return '—';
    return `${(v * 100).toFixed(d)}%`;
  },
  area(km2) {
    if (km2 === null || km2 === undefined) return '—';
    return km2 < 1 ? `${fmt.num(km2 * 100, 1)} ha` : `${fmt.num(km2, km2 < 10 ? 1 : 0)} km²`;
  },
  coord(lon, lat) { return `${lat.toFixed(5)}°N, ${lon.toFixed(5)}°E`; },
  /* Layer values carry very different magnitudes; pick sensible precision per unit. */
  value(v, units) {
    if (v === null || v === undefined) return '—';
    if (units === 'm') return `${fmt.num(v, 0)}`;
    if (units === 'mm/yr') return `${fmt.num(v, 0)}`;
    if (units === 'degrees') return v.toFixed(1);
    if (units === 'NDVI') return v.toFixed(3);
    return Math.abs(v) < 1 ? v.toFixed(4) : fmt.num(v, 2);
  },
};

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
