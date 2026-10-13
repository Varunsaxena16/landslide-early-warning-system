/* Entry point: boot, wire every control, own the interaction model. */
import { api, esc, fmt, debounce, setDataVersion } from './api.js?v=bulletin';
import { BASEMAPS } from './basemaps.js';
import { MapView } from './map.js?v=priority';
import { LayerPanel } from './layers.js';
import { Inspector } from './inspect.js';
import { StatsDrawer } from './stats.js';
import { ClassRail, Readout } from './rail.js';
import { applyTheme, currentTheme, mountThemePicker } from './theme.js';
import { Intro, prefersReducedMotion, runIntro } from './intro.js';
import { ListView } from './listview.js';
import { Bulletin } from './bulletin.js?v=bulletin2';

const $ = s => document.querySelector(s);

/* Default opacity for the risk surface over a tiled base map. */
const RISK_OPACITY = 0.46;

const state = {
  meta: null, stats: null, view: null, panel: null, inspector: null, drawer: null,
  rail: null, readout: null, list: null,
  places: [], invFacets: {}, invLoaded: false, invFilter: {}, mode: 'map',
  tab: 'risk', alertsByRank: {},
};

boot().catch(err => {
  $('#boot-msg').textContent = err.message;
  $('#boot-msg').style.color = '#ff7f32';
  console.error(err);
});

async function boot() {
  const msg = $('#boot-msg');
  applyTheme(currentTheme());

  msg.textContent = 'loading layer catalogue…';
  state.meta = await api.meta();
  setDataVersion(state.meta.data_version);

  msg.textContent = 'building the map…';
  state.view = await new MapView($('#map'), state.meta).ready;
  window.__map = state.view.map;                       // debugging handle

  state.panel = new LayerPanel(state.view, {
    groups: $('#layer-groups-model'),
    factors: $('#layer-groups-factors'),
    warning: $('#ew-rasters'),
    factorsCount: $('#factors-count'),
    legend: $('#legend-stack'),
  });
  state.inspector = new Inspector(state.view, {
    root: $('#inspector'), title: $('#insp-title'), sub: $('#insp-sub'),
    body: $('#insp-body'), close: $('#insp-close'),
  });
  state.drawer = new StatsDrawer(state.view, {
    root: $('#drawer'), body: $('#drawer-body'), close: $('#drawer-close'), btn: $('#btn-stats'),
  }, rank => state.inspector.showHotspot(rank));
  state.list = new ListView(
    { root: $('#listview'), track: $('#list-track'), sub: $('#list-sub') },
    rank => { setMode('map'); state.inspector.showHotspot(rank); });

  buildBasemaps();
  buildThemePicker();
  wireFactorsDisclosure();
  wireOverlays();
  wireSearch();
  wireMapInteractions();
  wireChrome();

  msg.textContent = 'loading hotspots…';
  // Roads, alerts and shelters are not needed to draw the risk map, and the
  // highway advisory is the slow call. Waiting for it here is what left the
  // boot screen on "loading hotspots…" and starved every tile request.
  const [, , , stats] = await Promise.all([
    loadHotspots(), loadPlaces(), loadFacets(), api.stats(), loadBorders(),
  ]);
  state.stats = stats;
  flushQueuedReports();

  // After loadBorders, so the mask has an outline to punch a hole in.
  wireFocus();

  state.readout = new Readout($('#readout'));
  state.bulletin = new Bulletin($('#bulletin'), focusBulletin);
  state.rail = new ClassRail($('#rail-inner'), stats, onRailChange);
  onRailChange(state.rail.current);

  await state.panel.toggle('lsm_class', true);
  // The risk surface is an overlay on a tiled ground, not the ground itself, so it
  // is held back far enough to read the terrain and the place names underneath.
  state.panel.setOpacity('lsm_class', RISK_OPACITY);
  // Risk-category tiles go out before the flight, and before roads or shelters.
  scheduleAfterRisk(state.view.prefetchRisk());

  // Hold the data down and put the camera out over the Pacific BEFORE the boot
  // screen lifts, so the first frame anyone sees is the globe, not the map.
  const intro = !prefersReducedMotion();
  if (intro) {
    state.intro = new Intro(state.view);
    state.intro.lower();
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  }

  $('#boot').classList.add('done');
  setTimeout(() => $('#boot').remove(), 950);

  if (intro) {
    await runIntro(state.intro);
    if (!state.intro.signal.cancelled) settleIntoSikkim();
  } else {
    state.setFocus?.(true);
  }
}

/* Risk tiles first. Highway advisory and assembly points next, so they are
   on the map when Early warning opens. Other rasters wait until those two
   responses are back and are no longer competing for the one worker. */
function scheduleAfterRisk(riskReady) {
  (riskReady || Promise.resolve())
    .catch(() => {})
    .then(() => warmSociety())
    .then(() => {
      warmBulletin();
      state.view.warmTiles((state.meta.layers || []).map(l => l.id));
    });
}

function focusBulletin(item) {
  if (item.lon != null && item.lat != null) {
    state.view.map.flyTo({
      center: [item.lon, item.lat],
      zoom: Math.max(state.view.map.getZoom(), 8.2),
      essential: true,
    });
    return;
  }
  try {
    const url = new URL(item.url || '');
    if (url.protocol === 'http:' || url.protocol === 'https:') {
      window.open(url.href, '_blank', 'noopener');
    }
  } catch { /* a quiet item has no link */ }
}

function warmBulletin() {
  const pull = () => api.bulletin().then(doc => state.bulletin?.set(doc)).catch(() => {});
  pull();
  setInterval(pull, 8 * 60 * 1000);
}

let forecastAsked = false;

function refreshForecast() {
  if (forecastAsked) return;
  forecastAsked = true;
  loadAlerts({ refresh: true }).then(() => {
    refreshEwCounts();
    if (state.tab === 'warning') syncEwLayers();
  }).catch(() => {});
}

function warmSociety() {
  const show = () => {
    refreshEwCounts();
    if (state.tab === 'warning') syncEwLayers();
  };
  loadAlerts().then(show).catch(() => {});
  return Promise.all([
    loadCorridor().then(show),
    loadShelters().then(show),
  ]);
}

/* ------------------------------------------------------- rail + readout --- */
function onRailChange(sel) {
  const s = state.stats;
  if (!s) return;
  if (state.tab === 'warning') return;

  if (sel.cls === null) {
    // "All" — the whole state
    state.view.setHotspotFilter(null);
    state.panel.setClassFilter(null);
    state.readout.show([
      { label: 'Hotspot patches', value: state.meta.counts.hotspots,
        note: 'Very-High, 8-connected' },
      { label: 'Very High', value: classRow(5).area_km2, unit: 'km²',
        note: `${classRow(5).share_pct}% of Sikkim` },
    ]);
  } else if (sel.cls === 5) {
    state.view.setHotspotFilter(null);
    state.panel.setClassFilter(5);
    state.readout.show([
      { label: 'Hotspot patches', value: state.meta.counts.hotspots, note: 'all shown' },
      { label: 'Mapped landslides', value: validRow(5)?.landslides ?? 0,
        note: `${fmt.pct(validRow(5)?.landslide_share ?? 0, 0)} of the GSI inventory` },
    ]);
  } else {
    // lower classes hold no hotspots by definition — show area and inventory instead
    state.view.setHotspotFilter(['==', ['get', 'rank_area'], -1]);
    state.panel.setClassFilter(sel.cls);
    const row = classRow(sel.cls), v = validRow(sel.cls);
    state.readout.show([
      { label: sel.label, value: row.area_km2, unit: 'km²', note: `${row.share_pct}% of Sikkim` },
      { label: 'Mapped landslides', value: v?.landslides ?? 0,
        note: v ? `density ${v.density_ratio}× versus area alone` : '' },
    ]);
  }

  if (state.mode === 'list') state.list.open(sel);
}

const classRow = c => state.stats.area.by_class.find(r => r.class === c) || {};
const validRow = c => state.stats.validation.by_class.find(r => r.class === c);

/* ---------------------------------------------------------------- theme -- */
function buildThemePicker() {
  mountThemePicker({ button: $('#btn-theme'), menu: $('#theme-drop') }, async () => {
    // Every panel, scrim and vignette is drawn from CSS custom properties and
    // repaints itself. The map does not: the base map's exposure lives in the
    // style, so the style has to be rebuilt for the new theme to reach it.
    await state.view.reloadStyle(restoreMapLayers);
    state.view.map.resize();
  });
}

/* --------------------------------------------------- factors disclosure --- */
function wireFactorsDisclosure() {
  const btn = $('#factors-toggle'), panel = $('#layer-groups-factors');
  const set = (open) => {
    panel.classList.toggle('is-collapsed', !open);
    btn.classList.toggle('is-open', open);
    btn.setAttribute('aria-expanded', String(open));
    try { localStorage.setItem('sikkim.factors', open ? '1' : '0'); } catch {}
  };
  let open = false;
  try { open = localStorage.getItem('sikkim.factors') === '1'; } catch {}
  set(open);
  btn.addEventListener('click', () => set(panel.classList.contains('is-collapsed')));
}

/* Everything that lives above the base-map style and has to be put back after the
   style is rebuilt. Both the base-map switch and the theme switch go through it. */
function restoreMapLayers() {
  state.panel.restore();
  state.view.restoreLiveRain();
  restoreOverlayData();
  if (state.rail) onRailChange(state.rail.current);
}

/* ------------------------------------------------------- study-area focus -- */
/* Masks everything outside Sikkim and drops the reference geography with it, so
   the map shows the area of interest and nothing else. */
function wireFocus() {
  const btn = $('#btn-focus');
  const set = (on, duration = 0) => {
    if (btn.classList.contains('is-on') === !!on) return;
    if (duration > 0) {
      btn.classList.add('is-settling');
      void btn.offsetWidth;
    } else {
      btn.classList.remove('is-settling');
    }
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-pressed', String(on));
    state.view.focusArea(on, duration);
    try { localStorage.setItem('sikkim.focus', on ? '1' : '0'); } catch { /* private mode */ }
    if (duration > 0) setTimeout(() => btn.classList.remove('is-settling'), duration + 40);
  };
  state.setFocus = set;
  // Off through the opening flight, so the neighbours are there to fade.
  set(false);
  btn.addEventListener('click', () => set(!btn.classList.contains('is-on')));
  state.view.map.on('moveend', () => {
    if (!btn.classList.contains('is-on')) return;
    const map = state.view.map;
    const c = map.getCenter();
    if (map.getZoom() < 3.8 || !inSikkim(c.lng, c.lat)) {
      set(false, prefersReducedMotion() ? 0 : FOCUS_MS);
    }
  });
}

function ringHas(ring, lng, lat) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1];
    const xj = ring[j][0], yj = ring[j][1];
    if ((yi > lat) !== (yj > lat) && lng < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

/* True when the point sits in the study-area polygon. Unknown outline stays
   "inside" so a missing file cannot turn the mask off by itself. */
function inSikkim(lng, lat) {
  const g = outlineFC?.features?.[0]?.geometry;
  if (!g) return true;
  const polys = g.type === 'Polygon' ? [g.coordinates]
    : g.type === 'MultiPolygon' ? g.coordinates : [];
  return polys.some(rings => ringHas(rings[0], lng, lat)
    && !rings.slice(1).some(hole => ringHas(hole, lng, lat)));
}

/* After the opening flight has landed, dissolve the surrounding states.
   The camera stays where the flight left it. The fade and the switch share
   one three-second run. */
const FOCUS_MS = 3000;

function settleIntoSikkim() {
  if (prefersReducedMotion()) {
    state.setFocus(true);
    return Promise.resolve();
  }
  return new Promise(resolve => {
    let skipped = false;
    const skip = (ev) => {
      if (!ev.isTrusted) return;
      skipped = true;
    };
    const events = ['pointerdown', 'wheel', 'keydown'];
    events.forEach(ev => window.addEventListener(ev, skip, { passive: true }));
    // The flight's own fade is still resolving when runIntro returns.
    setTimeout(() => {
      events.forEach(ev => window.removeEventListener(ev, skip));
      if (skipped || state.intro?.signal.cancelled) { resolve(); return; }
      state.setFocus(true, FOCUS_MS);
      setTimeout(resolve, FOCUS_MS + 40);
    }, 1700);
  });
}

/* ------------------------------------------------------------- basemaps -- */
function buildBasemaps() {
  $('#basemap-grid').innerHTML = BASEMAPS.map((b, i) => `
    <button class="bm ${i === 0 ? 'is-active' : ''}" data-id="${b.id}"
      ${b.swatch ? `style="background:${b.swatch}"` : ''}
      title="${esc(b.label)}"><span>${esc(b.label)}</span></button>`).join('');

  $('#basemap-grid').addEventListener('click', async e => {
    const btn = e.target.closest('.bm');
    if (!btn) return;
    $('#basemap-grid').querySelectorAll('.bm').forEach(b => b.classList.toggle('is-active', b === btn));
    await state.view.setBasemap(btn.dataset.id, restoreMapLayers);
    if (!state.panel.userSetOpacity.has('lsm_class') && state.panel.active.has('lsm_class')) {
      state.panel.setOpacity('lsm_class', RISK_OPACITY);
    }
  });
}

/* ------------------------------------------------------------- overlays -- */
let hotspotPoints = null, inventoryFC = null, placesFC = null, alertsFC = null;
let corridorFC = null, sheltersFC = null;
let bordersWorld = null, bordersRegion = null, outlineFC = null;

/* Country and state outlines. Static files rather than an API route — they never
   change with the data, and serving them from /data lets the browser cache them
   independently of the tile version. Failure is non-fatal: the map is still a map
   without its surroundings. */
async function loadBorders() {
  const grab = async (name) => {
    try {
      const r = await fetch(`/data/${name}.geojson`);
      return r.ok ? await r.json() : null;
    } catch { return null; }
  };
  [bordersWorld, bordersRegion, outlineFC] = await Promise.all([
    grab('borders-world'), grab('borders-region'), grab('sikkim'),
  ]);
  if (bordersWorld) state.view.setData('borders-world', bordersWorld);
  if (bordersRegion) state.view.setData('borders-region', bordersRegion);
  // The study-area outline drives the "Sikkim only" mask. Without it the toggle
  // has nothing to punch a hole in, so it stays disabled rather than blanking
  // the whole map.
  if (outlineFC) state.view.setOutline(outlineFC);
  $('#btn-focus').disabled = !outlineFC;
}

async function flushQueuedReports() {
  const key = 'sikkim.reports';
  let q;
  try { q = JSON.parse(localStorage.getItem(key) || '[]'); } catch { q = []; }
  if (!q.length) return;
  const left = [];
  for (const r of q) {
    try { await api.report(r); } catch { left.push(r); }
  }
  localStorage.setItem(key, JSON.stringify(left));
}

async function loadAlerts({ refresh = false } = {}) {
  try {
    const fc = await api.alerts(refresh ? { refresh: true } : {});
    alertsFC = fc;
    state.alertsByRank = Object.fromEntries(
      (fc.features || []).map(f => [String(f.properties.rank_area), f.properties]));
    state.view.setData('alerts', fc);
    state.inspector?.setAlerts(state.alertsByRank);
    const n = fc.meta?.active ?? (fc.features || []).length;
    const stale = fc.meta?.forecast_error;
    const count = $('#ly-alerts-n');
    if (count) count.textContent = stale && !n ? '—' : fmt.num(n);
    paintWarningDot(Boolean(stale) && !n);
    if (state.tab === 'warning') showWarningReadout();
    if ($('#ew-table')?.open) renderEwTable();
  } catch (e) {
    console.warn('alerts', e);
    if (!alertsFC) {
      const count = $('#ly-alerts-n');
      if (count) count.textContent = '—';
      paintWarningDot(true);
    }
  }
}

let corridorJob = null;
let shelterJob = null;

function loadCorridor() {
  if (corridorFC) return Promise.resolve(corridorFC);
  if (corridorJob) return corridorJob;
  corridorJob = api.corridor().then(fc => {
    corridorFC = fc;
    state.view.setData('corridor', fc);
    const c = fc.meta?.counts || {};
    const el = $('#ly-roads-n');
    if (el) el.textContent = `${c.avoid || 0} / ${c.caution || 0} / ${c.open || 0} km`;
    if ($('#ew-table')?.open) renderEwTable();
    return fc;
  }).catch(e => {
    corridorJob = null;
    console.warn('corridor', e);
  });
  return corridorJob;
}

function loadShelters() {
  if (sheltersFC) return Promise.resolve(sheltersFC);
  if (shelterJob) return shelterJob;
  shelterJob = api.shelters().then(fc => {
    sheltersFC = fc;
    state.view.setData('shelters', fc);
    const el = $('#ly-shelters-n');
    if (el) el.textContent = fmt.num(fc.meta?.count || fc.features?.length || 0);
    if ($('#ew-table')?.open) renderEwTable();
    return fc;
  }).catch(e => {
    shelterJob = null;
    console.warn('shelters', e);
  });
  return shelterJob;
}

async function loadHotspots() {
  const fc = await api.hotspots({ geometry: 'point', limit: 20000, order_by: 'area_ha' });
  hotspotPoints = fc;
  state.view.setData('hotspot-pt', fc);
  $('#ov-hotspots-n').textContent = `${fmt.num(fc.features.length)} Very-High patches`;
}

async function loadPlaces() {
  state.places = await api.places();
  placesFC = {
    type: 'FeatureCollection',
    features: state.places.map(p => ({
      type: 'Feature', properties: { name: p.name, type: p.type, district: p.district, note: p.note },
      geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
    })),
  };
  state.view.setData('places', placesFC);
}

async function loadFacets() {
  try { state.invFacets = await api.facets(); } catch { state.invFacets = {}; }
}

async function loadInventory() {
  const fc = await api.inventory({ limit: 5000, ...state.invFilter });
  inventoryFC = fc;
  state.view.setData('inventory', fc);
  $('#ov-inventory-n').textContent =
    `${fmt.num(fc.meta.returned)} of ${fmt.num(fc.meta.total)} shown`;
  const c = $('#inv-count');
  if (c) c.textContent = `${fmt.num(fc.meta.matched)} records match`;
}

function restoreOverlayData() {
  if (bordersWorld) state.view.setData('borders-world', bordersWorld);
  if (bordersRegion) state.view.setData('borders-region', bordersRegion);
  if (hotspotPoints) state.view.setData('hotspot-pt', hotspotPoints);
  if (inventoryFC) state.view.setData('inventory', inventoryFC);
  if (placesFC) state.view.setData('places', placesFC);
  if (alertsFC) state.view.setData('alerts', alertsFC);
  if (corridorFC) state.view.setData('corridor', corridorFC);
  if (sheltersFC) state.view.setData('shelters', sheltersFC);
  syncOverlayVisibility();
}

function syncOverlayVisibility() {
  state.view.setVisible(['hs-bloom', 'hs-halo', 'hs-core', 'hs-hit'], $('#ov-hotspots').checked);
  state.view.setVisible(['hotspot-fill', 'hotspot-glow', 'hotspot-line'], $('#ov-hotspot-outline').checked);
  state.view.setVisible(['inv-glow', 'inv-point'], $('#ov-inventory').checked);
  if (state.tab === 'warning') showWarningLayers(true);
  state.view.setVisible(['place-dot', 'place-label'], $('#ov-places').checked);
}

function wireOverlays() {
  $('#ov-hotspots').addEventListener('change', syncOverlayVisibility);
  $('#ov-places').addEventListener('change', syncOverlayVisibility);
  for (const id of ['ly-alerts', 'ly-roads', 'ly-shelters']) {
    document.getElementById(id)?.addEventListener('change', syncEwLayers);
  }
  $('#ew-table')?.addEventListener('toggle', () => { if ($('#ew-table').open) renderEwTable(); });
  $('#ew-table-layer')?.addEventListener('change', renderEwTable);

  $('#ov-hotspot-outline').addEventListener('change', async e => {
    syncOverlayVisibility();
    if (e.target.checked) await refreshOutlines();
  });

  $('#ov-inventory').addEventListener('change', async e => {
    syncOverlayVisibility();
    $('#inv-filters').hidden = !e.target.checked;
    if (e.target.checked) {
      if (!state.invLoaded) { renderInvFilters(); state.invLoaded = true; }
      await loadInventory();
    }
  });

  /* Outlines are fetched for the current viewport only — the full polygon set is
     ~8 MB and there is no reason to ship it to the browser. */
  state.view.map.on('moveend', debounce(() => {
    if ($('#ov-hotspot-outline').checked) refreshOutlines();
  }, 320));

  $('#btn-clear-layers').addEventListener('click', () => state.panel.clear());
}

async function refreshOutlines() {
  try {
    const fc = await api.hotspots({ bbox: state.view.bboxString(), limit: 900, order_by: 'area_ha' });
    state.view.setData('hotspot-poly', fc);
  } catch (e) { console.warn('outline fetch failed', e); }
}

function renderInvFilters() {
  const f = state.invFacets;
  const sel = (key, label) => {
    const opts = Object.entries(f[key] || {});
    if (!opts.length) return '';
    return `<div><label for="if-${key}">${esc(label)}</label>
      <select id="if-${key}" data-key="${key}">
        <option value="">All (${fmt.num(opts.reduce((s, [, n]) => s + n, 0))})</option>
        ${opts.map(([k, n]) => `<option value="${esc(k)}">${esc(k)} — ${n}</option>`).join('')}
      </select></div>`;
  };
  $('#inv-filters').innerHTML =
    sel('district', 'District') + sel('material_type', 'Material') +
    sel('movement_type', 'Movement') + sel('activity', 'Activity') +
    `<div><label for="if-risk">Model class at site</label>
      <select id="if-risk" data-key="risk_class">
        <option value="">Any</option>
        <option value="5">Very High</option><option value="4">High</option>
        <option value="3">Moderate</option><option value="2">Low</option><option value="1">Very Low</option>
      </select></div>
     <div class="inv-count" id="inv-count"></div>`;

  $('#inv-filters').querySelectorAll('select').forEach(s =>
    s.addEventListener('change', async () => {
      state.invFilter = {};
      $('#inv-filters').querySelectorAll('select').forEach(x => {
        if (x.value) state.invFilter[x.dataset.key] = x.value;
      });
      await loadInventory();
    }));
}

/* --------------------------------------------------------------- search -- */
function wireSearch() {
  const input = $('#search-input'), box = $('#search-results');
  let results = [], sel = -1;

  const run = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { box.hidden = true; return; }
    try {
      const r = await api.search(q);
      results = r.results; sel = -1;
      box.hidden = false;
      box.innerHTML = results.length ? results.map((x, i) => `
        <div class="sr" data-i="${i}">
          <span class="sr-ic" style="background:${x.kind === 'landslide' ? '#e9f2f0'
            : x.kind === 'hotspot' ? '#e07020' : '#8ebcb6'};color:${
            x.kind === 'landslide' ? '#e9f2f0' : x.kind === 'hotspot' ? '#e07020' : '#8ebcb6'}"></span>
          <span class="sr-tx"><b>${esc(x.label)}</b><small>${esc(x.sublabel || '')}</small></span>
        </div>`).join('') : `<div class="sr-empty">Nothing matches “${esc(q)}”</div>`;
    } catch { box.hidden = true; }
  }, 190);

  input.addEventListener('input', run);
  input.addEventListener('focus', () => {
    if (results.length && input.value.trim().length >= 2) box.hidden = false;
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { box.hidden = true; input.blur(); return; }
    if (!results.length || box.hidden) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
      box.querySelectorAll('.sr').forEach((n, i) => n.classList.toggle('is-sel', i === sel));
      box.querySelector('.sr.is-sel')?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      e.preventDefault();
      pick(results[sel < 0 ? 0 : sel]);
    }
  });

  box.addEventListener('click', e => {
    const n = e.target.closest('.sr');
    if (n) pick(results[Number(n.dataset.i)]);
  });

  document.addEventListener('click', e => {
    if (!e.target.closest('.search')) box.hidden = true;
  });

  function pick(r) {
    if (!r) return;
    box.hidden = true;
    input.blur();
    setMode('map');
    state.view.flyTo(r.lon, r.lat, r.kind === 'hotspot' ? 12 : 13.5);
    if (r.kind === 'hotspot') state.inspector.showHotspot(r.id);
    else if (r.kind === 'landslide') {
      api.inventory({ limit: 5000, full: true })
        .then(fc => {
          const f = fc.features.find(x => x.id === r.id);
          if (f) state.inspector.showLandslide(f);
          else state.inspector.showPoint(r.lon, r.lat, r.label);
        })
        .catch(() => state.inspector.showPoint(r.lon, r.lat, r.label));
    } else {
      state.inspector.showPoint(r.lon, r.lat, r.label);
    }
  }
}

/* ------------------------------------------------------ map interactions -- */
function wireMapInteractions() {
  const map = state.view.map;
  const CLICKABLE = ['alert-hit', 'hs-hit', 'inv-point', 'place-dot',
                     'corridor-hit', 'shelter-hit'];

  map.on('mousemove', e => {
    $('#badge-coords').textContent = fmt.coord(e.lngLat.lng, e.lngLat.lat);
    const hit = map.queryRenderedFeatures(e.point, { layers: CLICKABLE.filter(l => map.getLayer(l)) });
    map.getCanvas().style.cursor = hit.length ? 'pointer' : 'crosshair';
  });

  map.on('click', async e => {
    $('#map-hint')?.classList.add('gone');

    const hits = map.queryRenderedFeatures(e.point,
      { layers: CLICKABLE.filter(l => map.getLayer(l)) });

    if (hits.length) {
      const alertHit = hits.find(h => h.layer.id === 'alert-hit');
      const f = alertHit || hits[0];
      if (f.layer.id === 'alert-hit' || f.layer.id === 'hs-hit') {
        state.inspector.showHotspot(f.properties.rank_area);
        if (f.layer.id === 'alert-hit') openAlertTip(alertTip, alertFeature(f), true);
        else dismissAlertTip();
        return;
      }
      if (f.layer.id === 'corridor-hit') {
        dismissAlertTip();
        state.inspector.showCorridor(f.properties, f.geometry);
        return;
      }
      if (f.layer.id === 'shelter-hit') {
        dismissAlertTip();
        state.inspector.showShelter(f.properties);
        return;
      }
      if (f.layer.id === 'inv-point') {
        dismissAlertTip();
        const rec = await api.inventory({ limit: 5000, full: true })
          .then(fc => fc.features.find(x => x.id === f.id)).catch(() => null);
        if (rec) state.inspector.showLandslide(rec);
        else state.inspector.showPoint(...f.geometry.coordinates, 'Landslide');
        return;
      }
      if (f.layer.id === 'place-dot') {
        dismissAlertTip();
        state.inspector.showPoint(...f.geometry.coordinates, f.properties.name);
        return;
      }
    }
    state.inspector.showPoint(e.lngLat.lng, e.lngLat.lat);
    dismissAlertTip();
    if (!inSikkim(e.lngLat.lng, e.lngLat.lat)) {
      state.setFocus?.(false, prefersReducedMotion() ? 0 : FOCUS_MS);
    }
  });

  const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 13 });
  const alertTip = new maplibregl.Popup({
    closeButton: false, closeOnClick: false, offset: 16, maxWidth: '340px', className: 'alert-tip',
  });
  let alertTipTimer = null;
  const armAlertTipClose = () => {
    clearTimeout(alertTipTimer);
    alertTipTimer = setTimeout(() => alertTip.remove(), 220);
  };

  const numOrNull = (v) => {
    if (v == null || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };

  const alertFacts = (p) => {
    const p3 = numOrNull(p.precip_3h_mm);
    const p24 = numOrNull(p.precip_24h_mm);
    const soil = numOrNull(p.soil_moisture_0_to_7cm);
    const people = numOrNull(p.Population_Exposed ?? p.population);
    const buildings = numOrNull(p.Buildings_At_Risk ?? p.building_count);
    const area = numOrNull(p.area_km2);
    const risk = numOrNull(p.risk_score);
    const soilTxt = soil == null ? null
      : `soil moisture ${soil.toFixed(2)} (${soil >= 0.4 ? 'over' : 'under'} the 0.40 line)`;
    const rainTxt = (p3 != null || p24 != null)
      ? `rain ${p3 == null ? '—' : `${p3.toFixed(1)} mm`} in 3 hours and ${p24 == null ? '—' : `${p24.toFixed(1)} mm`} in 24 hours`
      : null;
    const held = [
      people == null ? null : `${fmt.num(people, 0)} people`,
      buildings == null ? null : `${fmt.num(buildings, 0)} buildings`,
    ].filter(Boolean).join(' and ');
    const where = [
      held || null,
      area == null ? null : `${area < 0.1 ? area.toFixed(3) : area.toFixed(2)} km²`,
      risk == null ? null : `risk ${risk.toFixed(0)}`,
    ].filter(Boolean).join(', ');
    const cause = [soilTxt, rainTxt].filter(Boolean).join(', ');
    const why = `Active on a Very-High slope${cause ? `: ${cause}` : ''}.${where ? ` ${where}.` : ''}`;
    return { p3, p24, soil, why };
  };

  const alertMeter = (label, value, line, cap, unit, critical) => {
    const v = value == null ? 0 : value;
    const top = Math.max(cap, v * 1.08, line);
    const pct = Math.max(0, Math.min(100, (v / top) * 100));
    const mark = (line / top) * 100;
    const crit = critical == null ? null : Math.max(0, Math.min(100, (critical / top) * 100));
    const cls = critical != null && v >= critical ? 'is-hot' : (v >= line ? 'is-on' : '');
    const shown = value == null ? '—' : (unit ? `${v.toFixed(1)} ${unit}` : v.toFixed(2));
    return `<div class="alert-meter ${cls}">
      <div class="alert-meter-top"><span>${esc(label)}</span><b>${esc(shown)}</b></div>
      <svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">
        <rect class="track" x="0" y="3" width="100" height="4"></rect>
        <rect class="fill" x="0" y="3" width="${pct.toFixed(2)}" height="4"></rect>
        <line class="mark" x1="${mark.toFixed(2)}" y1="0.6" x2="${mark.toFixed(2)}" y2="9.4" vector-effect="non-scaling-stroke"></line>
        ${crit == null ? '' : `<line class="crit" x1="${crit.toFixed(2)}" y1="0.6" x2="${crit.toFixed(2)}" y2="9.4" vector-effect="non-scaling-stroke"></line>`}
      </svg>
    </div>`;
  };

  let alertTipPinned = false;
  const dismissAlertTip = () => {
    alertTipPinned = false;
    clearTimeout(alertTipTimer);
    alertTip.remove();
  };
  const alertFeature = (f) => ({
    properties: { ...f.properties },
    geometry: { type: 'Point', coordinates: f.geometry.coordinates.slice() },
  });

  function openAlertTip(tip, feature, pin) {
    const p = feature.properties || {};
    const facts = alertFacts(p);
    const level = String(p.level || 'watch');
    const coords = feature.geometry?.coordinates;
    if (!coords) return;
    if (pin) alertTipPinned = true;
    tip.setLngLat(coords).setHTML(`
      <div class="alert-tip-card is-open" data-level="${esc(level)}">
        <div class="alert-kicker"><b>${esc(level)}</b><span>Hotspot #${esc(p.rank_area)}</span></div>
        <p class="alert-why">${esc(facts.why)}</p>
        <div class="alert-method">
          <b>How this alert is calculated</b>
          <p>Only a Very-High risk hotspot can alert. It turns on when rain over the next 3 hours reaches 10 mm, rain over the next 24 hours reaches 40 mm, or soil moisture in the top 7 cm reaches 0.40.</p>
          <p>Critical when 3-hour rain reaches 25 mm or 24-hour rain reaches 80 mm. Warning when the rain has crossed 10 mm or 40 mm. Soil on its own keeps the alert at Watch.</p>
          ${alertMeter('Rain, 3 h', facts.p3, 10, 30, 'mm', 25)}
          ${alertMeter('Rain, 24 h', facts.p24, 40, 100, 'mm', 80)}
          ${alertMeter('Soil, 0–7 cm', facts.soil, 0.4, 0.55, '', null)}
          <p class="alert-key">The dark tick is the line that makes the alert active. The red tick is the critical line. Readings are the Open-Meteo forecast at this hotspot.</p>
        </div>
      </div>`).addTo(map);
    const root = tip.getElement();
    root.onmouseenter = () => clearTimeout(alertTipTimer);
    root.onmouseleave = () => { if (!alertTipPinned) armAlertTipClose(); };
  }
  state.openAlertTip = (feature, pin = false) => openAlertTip(alertTip, feature, pin);

  map.on('mouseenter', 'place-dot', e => {
    const p = e.features[0].properties;
    pop.setLngLat(e.features[0].geometry.coordinates)
       .setHTML(`<b>${esc(p.name)}</b><br><span>${esc(p.type)} · ${esc(p.district)}</span>`).addTo(map);
  });
  map.on('mouseleave', 'place-dot', () => pop.remove());
  map.on('mouseenter', 'hs-hit', e => {
    const p = e.features[0].properties;
    pop.setLngLat(e.features[0].geometry.coordinates)
       .setHTML(`<b>Hotspot #${p.rank_area}</b><br><span>${fmt.area(p.area_km2)} ·
          risk ${Number(p.risk_score).toFixed(0)}</span>`).addTo(map);
  });
  map.on('mouseleave', 'hs-hit', () => pop.remove());
  map.on('mouseenter', 'alert-hit', e => {
    if (alertTipPinned) return;
    clearTimeout(alertTipTimer);
    openAlertTip(alertTip, alertFeature(e.features[0]), false);
  });
  map.on('mouseleave', 'alert-hit', () => { if (!alertTipPinned) armAlertTipClose(); });
  map.on('mouseenter', 'corridor-hit', e => {
    const p = e.features[0].properties;
    pop.setLngLat(e.lngLat)
       .setHTML(`<b>${esc(p.status)} · ${esc(p.ref || p.name || p.label || 'Highway')}</b><br><span>${esc(p.nearest_slide || p.action || '')}</span>`)
       .addTo(map);
  });
  map.on('mouseleave', 'corridor-hit', () => pop.remove());
  map.on('mouseenter', 'shelter-hit', e => {
    const p = e.features[0].properties;
    pop.setLngLat(e.features[0].geometry.coordinates)
       .setHTML(`<b>${esc(p.name)}</b><br><span>assembly point · ${esc(p.district || '')}</span>`)
       .addTo(map);
  });
  map.on('mouseleave', 'shelter-hit', () => pop.remove());
  map.on('mouseenter', 'inv-point', e => {
    const p = e.features[0].properties;
    pop.setLngLat(e.features[0].geometry.coordinates)
       .setHTML(`<b>${esc(p.slide_name || p.slide_no || 'Landslide')}</b><br>
         <span>${esc(p.material_type || '')} ${esc(p.movement_type || '')} · ${esc(p.district || '')}</span>`)
       .addTo(map);
  });
  map.on('mouseleave', 'inv-point', () => pop.remove());

  $('#zoom-in').addEventListener('click', () => map.zoomIn({ duration: 420 }));
  $('#zoom-out').addEventListener('click', () => map.zoomOut({ duration: 420 }));
}

/* ------------------------------------------------------------ map / list -- */
function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  const isList = mode === 'list';
  $('#view-map').classList.toggle('is-on', !isList);
  $('#view-list').classList.toggle('is-on', isList);
  if (isList) state.list.open(state.rail?.current);
  else state.list.close();
}

function setTab(name) {
  if (name === state.tab) return;
  state.tab = name;
  document.querySelectorAll('.panels [data-panel]').forEach(t => {
    const on = t.dataset.panel === name;
    t.classList.toggle('is-on', on);
    t.setAttribute('aria-selected', String(on));
  });
  const warning = name === 'warning';
  document.body.classList.toggle('is-warning', warning);
  fadeText(
    document.querySelector('#sidebar .sidebar-head h2'),
    warning ? 'Early warning' : 'Layers',
  );
  if (warning && $('#sidebar').classList.contains('is-closed')) {
    $('#sidebar-reopen').click();
  }
  const scroller = document.querySelector('#sidebar .sidebar-scroll');
  if (warning && scroller) {
    scroller.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
  }
  fadeText(
    $('#brand-sub'),
    warning ? 'alerts, roads, shelter — while it rains' : 'who is on the slope — before it moves',
  );
  showWarningLayers(warning);
  if (warning) {
    refreshEwCounts();
    if (state.readout) showWarningReadout();
    if (!alertsFC) {
      loadAlerts().then(() => refreshForecast()).catch(() => {});
    } else {
      refreshForecast();
    }
    if (!corridorFC) loadCorridor().then(() => { refreshEwCounts(); syncEwLayers(); }).catch(() => {});
    if (!sheltersFC) loadShelters().then(() => { refreshEwCounts(); syncEwLayers(); }).catch(() => {});
  } else if (state.rail) {
    onRailChange(state.rail.current);
  }
}

const ALERT_LAYERS = ['alert-radar-a', 'alert-radar-b', 'alert-bloom', 'alert-core', 'alert-hit'];
const ROAD_LAYERS = ['corridor-case', 'corridor-line', 'corridor-hit'];
const SHELTER_LAYERS = ['shelter-core', 'shelter-hit', 'shelter-label'];

function showWarningLayers(on) {
  state.view.fadeLayers(ALERT_LAYERS, on && !!$('#ly-alerts')?.checked);
  state.view.fadeLayers(ROAD_LAYERS, on && !!$('#ly-roads')?.checked);
  state.view.fadeLayers(SHELTER_LAYERS, on && !!$('#ly-shelters')?.checked);
}

function syncEwLayers() {
  if (state.tab !== 'warning') return;
  showWarningLayers(true);
}

/* The subtitle and the drawer title cross-fade the way the readout rises. */
function fadeText(el, next) {
  if (!el || el.textContent === next) return;
  if (prefersReducedMotion()) {
    el.textContent = next;
    return;
  }
  if (el._fadeSwap) el.removeEventListener('animationend', el._fadeSwap);
  el.classList.remove('is-leaving', 'is-arriving');
  void el.offsetWidth;
  el.classList.add('is-leaving');
  const swap = (ev) => {
    if (ev.target !== el) return;
    el.removeEventListener('animationend', swap);
    el._fadeSwap = null;
    el.textContent = next;
    el.classList.remove('is-leaving');
    void el.offsetWidth;
    el.classList.add('is-arriving');
  };
  el._fadeSwap = swap;
  el.addEventListener('animationend', swap);
}

function refreshEwCounts() {
  const n = alertsFC?.meta?.active ?? alertsFC?.features?.length;
  const a = $('#ly-alerts-n');
  if (a && n != null) a.textContent = fmt.num(n);
  const c = corridorFC?.meta?.counts;
  const r = $('#ly-roads-n');
  if (r && c) r.textContent = `${c.avoid || 0} / ${c.caution || 0} / ${c.open || 0} km`;
  const s = $('#ly-shelters-n');
  if (s && sheltersFC) s.textContent = fmt.num(sheltersFC.meta?.count || sheltersFC.features?.length || 0);
}

function paintWarningDot(failed) {
  const dot = document.querySelector('[data-panel="warning"] .dot');
  if (!dot) return;
  dot.classList.remove('ok', 'warn', 'bad');
  const feats = alertsFC?.features || [];
  if (failed && !feats.length) dot.classList.add('bad');
  else if (feats.some(f => f.properties.level === 'critical')) dot.classList.add('bad');
  else if (feats.some(f => f.properties.level === 'warning')) dot.classList.add('warn');
  else dot.classList.add('ok');
}

function showWarningReadout() {
  if (!state.readout) return;
  const feats = alertsFC?.features || [];
  const n = level => feats.filter(f => f.properties.level === level).length;
  state.readout.show([
    { label: 'Active alerts', value: feats.length,
      note: alertsFC ? 'rain or soil trigger' : 'forecast unavailable' },
    { label: 'Watch', value: n('watch'),
      note: `${n('warning')} warning · ${n('critical')} critical` },
  ]);
}

function renderEwTable() {
  const body = $('#ew-table-body');
  const which = $('#ew-table-layer')?.value || 'alerts';
  if (!body) return;
  if (which === 'roads') {
    const grouped = new Map();
    for (const f of corridorFC?.features || []) {
      const p = f.properties;
      const key = p.ref || p.name || p.highway || 'road';
      const row = grouped.get(key) || { key, avoid: 0, caution: 0, open: 0, feature: f };
      row[p.status] = (row[p.status] || 0) + 1;
      const rank = { avoid: 0, caution: 1, open: 2 };
      if ((rank[p.status] ?? 9) < (rank[row.feature.properties.status] ?? 9)) row.feature = f;
      grouped.set(key, row);
    }
    const rows = [...grouped.values()].sort((a, b) => b.avoid - a.avoid || b.caution - a.caution);
    body.innerHTML = rows.map(r => `<button type="button" class="ew-line" data-kind="road" data-id="${r.feature.properties.id}">
      <span>${esc(r.key)}</span><span></span><span>${r.avoid} avoid · ${r.caution} caution</span>
    </button>`).join('') || `<div class="note-box">No highway sections loaded.</div>`;
  } else if (which === 'shelters') {
    body.innerHTML = (sheltersFC?.features || []).map(f => {
      const p = f.properties;
      return `<button type="button" class="ew-line" data-kind="shelter" data-lon="${p.lon}" data-lat="${p.lat}">
        <span>${esc(p.kind)}</span><span>${esc(p.name)}</span><span></span>
      </button>`;
    }).join('') || `<div class="note-box">No assembly points loaded.</div>`;
  } else {
    const rows = (alertsFC?.features || []).slice().sort((a, b) =>
      (b.properties.risk_score || 0) - (a.properties.risk_score || 0));
    body.innerHTML = rows.map(f => {
      const p = f.properties;
      return `<button type="button" class="ew-line" data-kind="alert" data-rank="${p.rank_area}">
        <span>${esc(p.level)}</span><span>#${p.rank_area}</span><span>${p.risk_score == null ? '—' : Number(p.risk_score).toFixed(0)}</span>
      </button>`;
    }).join('') || `<div class="note-box">No alerts loaded.</div>`;
  }
  body.querySelectorAll('.ew-line').forEach(el => {
    el.addEventListener('click', () => {
      setMode('map');
      if (el.dataset.kind === 'alert') {
        const rank = Number(el.dataset.rank);
        const f = (alertsFC?.features || []).find(x => Number(x.properties.rank_area) === rank);
        state.inspector.showHotspot(rank);
        if (f) {
          const [lon, lat] = f.geometry.coordinates;
          state.view.flyTo(lon, lat, Math.max(state.view.map.getZoom(), 9.5));
          state.openAlertTip?.(f, true);
        }
      } else if (el.dataset.kind === 'road') {
        const f = (corridorFC?.features || []).find(x => String(x.properties.id) === el.dataset.id);
        if (f) state.inspector.showCorridor(f.properties, f.geometry);
      } else {
        const lon = Number(el.dataset.lon), lat = Number(el.dataset.lat);
        const f = (sheltersFC?.features || []).find(x => x.properties.lon === lon && x.properties.lat === lat);
        if (f) {
          state.view.flyTo(lon, lat, 13);
          state.inspector.showShelter(f.properties);
        }
      }
    });
  });
}

/* --------------------------------------------------------------- chrome -- */
function wireChrome() {
  const sidebar = $('#sidebar'), btnLayers = $('#btn-layers'), reopen = $('#sidebar-reopen');
  // The drawer overlays the map rather than shrinking it, so the canvas never
  // resizes; only the camera padding has to follow.
  let booted = false;
  const setSidebar = (open) => {
    sidebar.classList.toggle('is-closed', !open);
    document.body.classList.toggle('sidebar-closed', !open);
    btnLayers.classList.toggle('is-on', open);
    btnLayers.setAttribute('aria-expanded', String(open));
    reopen.hidden = open;
    try { localStorage.setItem('sikkim.layers', open ? '1' : '0'); } catch {}
    // On a later toggle, ease the padding so the user keeps their zoom and pan.
    // At boot there is nothing to preserve, and easing here would undo the refit.
    if (booted) setTimeout(() => state.view.applyChromePadding(open), 380);
  };
  // Open by default on desktop so nothing is hidden on first view; one click gives
  // the full map. On a phone the drawer would cover everything, so it starts closed.
  let open = window.innerWidth > 900;
  try { const v = localStorage.getItem('sikkim.layers'); if (v !== null) open = v === '1'; } catch {}
  setSidebar(open);
  state.view.refit(open, false);
  booted = true;
  btnLayers.addEventListener('click', () => setSidebar(sidebar.classList.contains('is-closed')));
  $('#sidebar-close').addEventListener('click', () => setSidebar(false));
  reopen.addEventListener('click', () => setSidebar(true));

  $('#view-map').addEventListener('click', () => setMode('map'));
  $('#view-list').addEventListener('click', () => setMode('list'));

  $('#btn-stats').addEventListener('click', () => {
    state.drawer.toggle();
    $('#btn-stats').classList.toggle('is-on', !$('#drawer').hidden);
  });
  $('#btn-about').addEventListener('click', () => { $('#about').hidden = false; renderAbout(); });
  $('#about-close').addEventListener('click', () => { $('#about').hidden = true; });
  $('#about').addEventListener('click', e => { if (e.target.id === 'about') $('#about').hidden = true; });
  const hint = $('#map-hint');
  const dismissHint = () => hint.classList.add('gone');
  $('#map-hint .hint-x').addEventListener('click', dismissHint);
  // Start the 6 s depletion on the next frame so the transition actually runs -
  // setting the class in the same frame as the initial paint would skip it.
  requestAnimationFrame(() => requestAnimationFrame(() => hint.classList.add('counting')));
  setTimeout(dismissHint, 6000);

  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== $('#search-input')) {
      e.preventDefault(); $('#search-input').focus();
    }
    if (e.key === 'Escape') {
      $('#about').hidden = true;
      if (!$('#drawer').hidden) { state.drawer.close(); $('#btn-stats').classList.remove('is-on'); }
      else if (!$('#inspector').hidden) state.inspector.hide();
    }
    // ↑/↓ step the class rail, like the year timeline
    if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && state.rail
        && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || '')) {
      e.preventDefault();
      const next = state.rail.active + (e.key === 'ArrowDown' ? 1 : -1);
      if (next >= 0 && next < state.rail.items.length) state.rail.select(next);
    }
  });

  document.querySelectorAll('.panels [data-panel]').forEach(t => t.addEventListener('click', () => {
    setTab(t.dataset.panel);
  }));

  $('#report-form')?.addEventListener('submit', async ev => {
    ev.preventDefault();
    const c = state.view.map.getCenter();
    const body = {
      sign: $('#report-sign').value,
      note: $('#report-note').value,
      lon: c.lng,
      lat: c.lat,
    };
    const key = 'sikkim.reports';
    try {
      await api.report(body);
      showToast('Report sent', 'Unverified. If this sits on a hotspot the alert rises one level.');
      $('#report-note').value = '';
      loadAlerts().catch(() => {});
    } catch (e) {
      const q = JSON.parse(localStorage.getItem(key) || '[]');
      q.push(body);
      localStorage.setItem(key, JSON.stringify(q));
      showToast('Saved offline', 'The report will send when the API is reachable.');
    }
  });
}

/* --------------------------------------------------------------- toast ---- */
let toastTimer = null;
function showToast(title, msg, ms = 2600) {
  const el = $('#toast');
  clearTimeout(toastTimer);
  $('#toast-title').textContent = title;
  $('#toast-msg').textContent = msg;
  el.classList.remove('leaving');
  el.hidden = false;
  const dismiss = () => {
    el.classList.add('leaving');
    setTimeout(() => { el.hidden = true; el.classList.remove('leaving'); }, 300);
  };
  toastTimer = setTimeout(dismiss, ms);
  el.onclick = () => { clearTimeout(toastTimer); dismiss(); };
}

function renderAbout() {
  const TEAM = ['Varun', 'Aman', 'Kajal', 'Sameera', 'Vanshaj', 'Dheeraj'];
  $('#about-body').innerHTML = `
    <div class="credit-hero">
      <div class="credit-title">SIH GI 2026</div>
    </div>
    <div class="team">
      ${TEAM.map((n, i) => `
        <div class="team-member" style="animation-delay:${i * 70}ms">
          <span class="team-dot"></span><span class="team-name">${esc(n)}</span>
        </div>`).join('')}
    </div>`;
}
