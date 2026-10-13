/* Map construction and every layer that lives on it.

   The hotspot markers are the visual centrepiece. They are built as three stacked
   circle layers rather than sprite images: an outer bloom, a mid halo and a hard
   core, each with `circle-blur`. That renders a real luminous point on the GPU for
   every published patch at once, and the radii can be driven straight off risk_score
   (susceptibility × exposure) so a brighter dot means more is at stake.

   A requestAnimationFrame loop breathes the bloom radius. It is a slow sine — the
   point is to make the map feel alive, not to distract from it. */
import { BASEMAPS, baseStyle } from './basemaps.js';
import { api } from './api.js';
import { groundIsLight } from './theme.js';

export const RASTER_ROOT = 'zzz-raster-anchor';

/* Borders are chrome, so they follow the theme rather than sitting at a fixed
   white that would glare on the light theme. The literal here is only the initial
   value; applyMarkerTheme() repaints it. */
const BORDER_COLOR = '#8ebcb6';

/* Bloom radius at zoom 11, from the smallest published risk to a score of 100.
   The pulse and the layer paint both read these so they cannot drift. */
const BLOOM_LO = 9;
const BLOOM_HI = 38;

/* A 15-degree graticule, built rather than shipped - it is a few hundred bytes of
   generated line and only ever visible on the globe. */
function graticule(step = 15) {
  const features = [];
  for (let lon = -180; lon <= 180; lon += step) {
    const line = [];
    for (let lat = -80; lat <= 80; lat += 2) line.push([lon, lat]);
    features.push({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: line } });
  }
  for (let lat = -75; lat <= 75; lat += step) {
    const line = [];
    for (let lon = -180; lon <= 180; lon += 2) line.push([lon, lat]);
    features.push({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: line } });
  }
  return { type: 'FeatureCollection', features };
}

export class MapView {
  constructor(el, meta) {
    this.meta = meta;
    this.basemap = BASEMAPS[0];
    const [w, s, e, n] = meta.grid.bounds_wgs84;

    this.map = new maplibregl.Map({
      container: el,
      style: baseStyle(this.basemap, MapView.styleOpts()),
      bounds: [[w, s], [e, n]],
      // Desktop leaves room for the drawer, the rail and the readout; a phone has
      // none of them, and the desktop padding would push Sikkim off the screen.
      fitBoundsOptions: { padding: window.innerWidth < 900
        ? { top: 24, bottom: 96, left: 18, right: 18 }
        : { top: 56, bottom: 84, left: 388, right: 250 } },
      // No maxBounds, and a minZoom that stops where the globe fills the frame
      // rather than floating small inside it. Sikkim is still where every camera
      // move lands.
      minZoom: 1.4, maxZoom: 16, attributionControl: false,
      dragRotate: false, pitchWithRotate: false, fadeDuration: 220,
    });
    window.__map = this.map;
    this.map.on('error', (ev) => { window.__mapErr = String(ev?.error?.message || ev?.error || ev); });
    this.map.addControl(new maplibregl.ScaleControl({ maxWidth: 96, unit: 'metric' }), 'bottom-right');
    this.map.touchZoomRotate.disableRotation();

    this.rasterOrder = [];
    this.pulse = 0;
    this.ready = new Promise((res, rej) => this.map.on('load', () => {
      try {
        this._scaffold();
        this.applyMarkerTheme();
        this._startPulse();
        res(this);
      } catch (err) {
        rej(err);
      }
    }));
  }

  _scaffold() {
    const m = this.map;
    const empty = { type: 'FeatureCollection', features: [] };

    /* ---- reference geography -------------------------------------------
       Country and state outlines so the ground around Sikkim is not a void, and
       a graticule that only means anything once the map is a globe. Added before
       the anchor, so factor rasters draw over them: the data is the subject and
       these are context. */
    m.addSource('graticule', { type: 'geojson', data: graticule() });
    m.addLayer({ id: 'graticule-line', type: 'line', source: 'graticule', paint: {
      'line-color': BORDER_COLOR,
      'line-width': 0.5,
      // Only on the globe. Flat, a graticule is just clutter over the data.
      'line-opacity': ['interpolate', ['linear'], ['zoom'], 0, 0.35, 3, 0.22, 5, 0],
    } });

    m.addSource('borders-world', { type: 'geojson', data: empty });
    m.addLayer({ id: 'border-country', type: 'line', source: 'borders-world', paint: {
      'line-color': BORDER_COLOR,
      'line-width': ['interpolate', ['linear'], ['zoom'], 0, 0.6, 4, 0.9, 9, 1],
      // Fades out as border-admin1 fades in: both carry national borders, and the
      // 1:110m version would otherwise ghost alongside the 1:10m one.
      'line-opacity': ['interpolate', ['linear'], ['zoom'], 1.4, 0.8, 4, 0.62, 6, 0.16, 8, 0.07],
    } });

    m.addSource('borders-region', { type: 'geojson', data: empty });
    m.addLayer({ id: 'border-admin1', type: 'line', source: 'borders-region', paint: {
      'line-color': BORDER_COLOR,
      'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.5, 8, 0.85, 12, 1.2],
      // Takes over from the coarse outlines as soon as it is worth drawing.
      'line-opacity': ['interpolate', ['linear'], ['zoom'], 3.5, 0, 5, 0.42, 7, 0.6, 11, 0.5],
    } });

    /* The study-area boundary, drawn only while "Sikkim only" is on. With the base
       map hidden the risk surface would otherwise end on a raw raster edge; this
       gives the state its own outline to end on. */
    m.addSource('aoi', { type: 'geojson', data: empty });
    m.addLayer({ id: 'aoi-line', type: 'line', source: 'aoi',
      layout: { visibility: 'none', 'line-join': 'round' },
      paint: { 'line-color': BORDER_COLOR, 'line-width': 1.2, 'line-opacity': 0.9 } });

    m.addSource('anchor', { type: 'geojson', data: empty });
    m.addLayer({ id: RASTER_ROOT, type: 'circle', source: 'anchor',
                 paint: { 'circle-radius': 0, 'circle-opacity': 0 } });

    /* ---- hotspot outlines ----
       A thin maroon line with a narrow orange blur under it. The blur is only
       wide enough to lift the edge off the susceptibility surface. */
    m.addSource('hotspot-poly', { type: 'geojson', data: empty });
    m.addLayer({ id: 'hotspot-fill', type: 'fill', source: 'hotspot-poly',
      paint: { 'fill-color': '#c45c28', 'fill-opacity': 0.08 }, layout: { visibility: 'none' } });
    m.addLayer({ id: 'hotspot-glow', type: 'line', source: 'hotspot-poly',
      paint: { 'line-color': '#f08a2e', 'line-opacity': 0.28, 'line-blur': 2.5,
               'line-width': ['interpolate', ['linear'], ['zoom'], 8, 2.2, 14, 4.5] },
      layout: { visibility: 'none', 'line-join': 'round', 'line-cap': 'round' } });
    m.addLayer({ id: 'hotspot-line', type: 'line', source: 'hotspot-poly',
      paint: { 'line-color': '#7c2d12', 'line-opacity': 0.9,
               'line-width': ['interpolate', ['linear'], ['zoom'], 8, 0.8, 14, 1.7] },
      layout: { visibility: 'none', 'line-join': 'round' } });

    /* ---- luminous hotspot points ------------------------------------------
       Radius scales with risk_score (susceptibility × exposure), so the eye is
       drawn to the patches where a failure would matter, not the largest slope.

       MapLibre only accepts `zoom` at the top level of an interpolate, so the
       two inputs are nested rather than multiplied: zoom on the outside, the
       exposure ramp as each stop's output. */
    const glow = (lo, hi) => ['interpolate', ['linear'], ['zoom'],
      7,  ['interpolate', ['linear'], ['get', 'risk_score'], 0, lo * 0.55, 100, hi * 0.55],
      9,  ['interpolate', ['linear'], ['get', 'risk_score'], 0, lo * 0.78, 100, hi * 0.78],
      11, ['interpolate', ['linear'], ['get', 'risk_score'], 0, lo, 100, hi],
      15, ['interpolate', ['linear'], ['get', 'risk_score'], 0, lo * 1.7, 100, hi * 1.7]];
    const zScale = (base) => ['interpolate', ['linear'], ['zoom'], 7, base * 0.85, 11, base, 15, base * 1.7];

    m.addSource('hotspot-pt', { type: 'geojson', data: empty });

    /* Risk is spread well below 100 (it is a product, not a percentile), so the
       radii have to use the full 0–100 ramp or every dot looks the same size. */
    m.addLayer({ id: 'hs-bloom', type: 'circle', source: 'hotspot-pt', paint: {
      'circle-color': '#e07020',
      'circle-radius': glow(BLOOM_LO, BLOOM_HI), 'circle-blur': 1.5, 'circle-opacity': 0.18,
      'circle-radius-transition': { duration: 0 },
    } });
    m.addLayer({ id: 'hs-halo', type: 'circle', source: 'hotspot-pt', paint: {
      'circle-color': '#f08a2e',
      'circle-radius': glow(3.8, 14), 'circle-blur': 0.8, 'circle-opacity': 0.38,
    } });
    m.addLayer({ id: 'hs-core', type: 'circle', source: 'hotspot-pt', paint: {
      'circle-color': '#7c2d12',
      'circle-radius': glow(2.6, 6.8), 'circle-blur': 0.05, 'circle-opacity': 1,
      'circle-stroke-color': '#f08a2e', 'circle-stroke-width': 1.35, 'circle-stroke-opacity': 0.75,
    } });
    // generous invisible hit target — the visible core is only a few pixels
    m.addLayer({ id: 'hs-hit', type: 'circle', source: 'hotspot-pt',
      paint: { 'circle-radius': zScale(18), 'circle-opacity': 0 } });

    /* ---- active alerts (rain / soil on a risk hotspot) ---- */
    const alertColor = ['match', ['get', 'level'],
      'critical', '#b91c1c', 'warning', '#ea580c', '#ca8a04'];
    const highRisk = ['>=', ['to-number', ['get', 'risk_score']], 40];
    m.addSource('alerts', { type: 'geojson', data: empty });
    /* Expanding rings on the high-risk alert nodes. Two phases, so a second
       sweep starts before the first fades. */
    for (const id of ['alert-radar-a', 'alert-radar-b']) {
      m.addLayer({ id, type: 'circle', source: 'alerts', filter: highRisk,
        layout: { visibility: 'none' },
        paint: {
          'circle-radius': 8,
          'circle-color': 'rgba(0,0,0,0)',
          'circle-stroke-color': '#f0c14a',
          'circle-stroke-width': 1.6,
          'circle-opacity': 0,
          'circle-stroke-opacity': 0.7,
        } });
    }
    m.addLayer({ id: 'alert-bloom', type: 'circle', source: 'alerts',
      layout: { visibility: 'none' },
      paint: {
      'circle-color': alertColor,
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 10, 11, 18, 15, 28],
      'circle-blur': 1.1, 'circle-opacity': 0.32,
    } });
    m.addLayer({ id: 'alert-core', type: 'circle', source: 'alerts',
      layout: { visibility: 'none' },
      paint: {
      'circle-color': alertColor,
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 3.5, 11, 6, 15, 8],
      'circle-opacity': 0.95,
      'circle-stroke-color': '#fff7ed', 'circle-stroke-width': 1.4,
    } });
    m.addLayer({ id: 'alert-hit', type: 'circle', source: 'alerts',
      layout: { visibility: 'none' },
      paint: { 'circle-radius': zScale(16), 'circle-opacity': 0 } });

    const roadColor = ['match', ['get', 'status'],
      'avoid', '#b91c1c', 'caution', '#ca8a04', '#3f7d6a'];
    m.addSource('corridor', { type: 'geojson', data: empty });
    m.addLayer({ id: 'corridor-case', type: 'line', source: 'corridor',
      layout: { visibility: 'none', 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': '#1c1917',
               'line-width': ['interpolate', ['linear'], ['zoom'], 6, 3.4, 9, 5.2, 13, 8],
               'line-opacity': 0.45 } });
    m.addLayer({ id: 'corridor-line', type: 'line', source: 'corridor',
      layout: { visibility: 'none', 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': roadColor,
               'line-width': ['interpolate', ['linear'], ['zoom'], 6, 1.8, 9, 3.4, 13, 6],
               'line-opacity': 0.95 } });
    m.addLayer({ id: 'corridor-hit', type: 'line', source: 'corridor',
      layout: { visibility: 'none', 'line-join': 'round' },
      paint: { 'line-color': roadColor, 'line-width': 14, 'line-opacity': 0 } });

    m.addSource('shelters', { type: 'geojson', data: empty });
    m.addLayer({ id: 'shelter-core', type: 'circle', source: 'shelters',
      layout: { visibility: 'none' },
      paint: { 'circle-color': '#1f6b56',
               'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 4, 10, 6.5, 13, 9],
               'circle-stroke-color': '#f4fbf8', 'circle-stroke-width': 1.6 } });
    m.addLayer({ id: 'shelter-hit', type: 'circle', source: 'shelters',
      layout: { visibility: 'none' },
      paint: { 'circle-radius': 16, 'circle-opacity': 0 } });
    m.addLayer({ id: 'shelter-label', type: 'symbol', source: 'shelters',
      minzoom: 8,
      layout: { visibility: 'none', 'text-field': ['get', 'name'], 'text-size': 10.5,
                'text-font': ['Noto Sans Regular'], 'text-offset': [0, 1.15],
                'text-anchor': 'top' },
      paint: { 'text-color': '#d7ebe3', 'text-halo-color': 'rgba(0,26,22,.85)',
               'text-halo-width': 1.2 } });

    /* ---- GSI inventory ---- */
    m.addSource('inventory', { type: 'geojson', data: empty });
    m.addLayer({ id: 'inv-glow', type: 'circle', source: 'inventory',
      paint: { 'circle-color': '#a8cbc7', 'circle-blur': 1.1, 'circle-opacity': 0.42,
               'circle-radius': ['interpolate', ['linear'], ['zoom'], 5.5, 0, 8, 5, 14, 11] },
      layout: { visibility: 'none' } });
    m.addLayer({ id: 'inv-point', type: 'circle', source: 'inventory',
      paint: { 'circle-color': '#e9f2f0', 'circle-opacity': 0.9, 'circle-blur': 0.2,
               'circle-radius': ['interpolate', ['linear'], ['zoom'], 5.5, 0, 8, 1.5, 14, 3.6] },
      layout: { visibility: 'none' } });

    /* ---- settlements ---- */
    m.addSource('places', { type: 'geojson', data: empty });
    m.addLayer({ id: 'place-dot', type: 'circle', source: 'places', paint: {
      'circle-color': '#8ebcb6', 'circle-opacity': 0.95,
      'circle-stroke-color': 'rgba(0,36,31,.8)',
      /* Collapsed to nothing below z6. Fifty settlements inside one state become
         a single clot at globe scale, and their dark stroke rings read as a black
         smudge sitting on the planet. Radius rather than opacity, so the intro's
         opacity fade stays free to drive the same layer. */
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 0, 6.4, 3],
      'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 5, 0, 6.4, 1],
    } });
    m.addLayer({ id: 'place-label', type: 'symbol', source: 'places',
      minzoom: 5.6,
      layout: { 'text-field': ['get', 'name'], 'text-size': 10.5,
                'text-font': ['Noto Sans Regular'], 'text-offset': [0, 1.1],
                'text-anchor': 'top', 'text-optional': true, 'text-letter-spacing': 0.06 },
      paint: { 'text-color': 'rgba(207,227,224,.72)', 'text-halo-color': 'rgba(0,26,22,.85)',
               'text-halo-width': 1.3 } });

    /* ---- selection highlight ---- */
    m.addSource('sel', { type: 'geojson', data: empty });
    m.addLayer({ id: 'sel-fill', type: 'fill', source: 'sel',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': '#ff7f32', 'fill-opacity': 0.16 } });
    m.addLayer({ id: 'sel-line', type: 'line', source: 'sel',
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'line-color': '#ff7f32', 'line-width': 1.6 } });
    m.addLayer({ id: 'sel-ring', type: 'circle', source: 'sel',
      filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 13, 'circle-color': 'rgba(255,127,50,.14)',
               'circle-stroke-width': 1, 'circle-stroke-color': '#ff7f32' } });
    m.addLayer({ id: 'sel-dot', type: 'circle', source: 'sel',
      filter: ['==', ['geometry-type'], 'Point'],
      paint: { 'circle-radius': 2.6, 'circle-color': '#fff' } });
  }

  /* Marker colours follow the theme.

     Both grounds keep a maroon core and a quiet orange bloom. On a dark map the
     bloom is only a little stronger, so the point still reads as warm rather than
     as a lamp. Outlines are the same pair: a maroon stroke with a narrow orange
     blur under it. It keys off the ground, not the theme: a dark UI showing
     full-strength tiles needs the light-ground treatment. */
  markerPalette() {
    const light = groundIsLight();
    return light
      ? { bloom: '#e07020', halo: '#f08a2e', core: '#7c2d12',
          bloomOp: 0.18, haloOp: 0.32, coreOp: 1,
          fill: '#c45c28', fillOp: 0.08,
          glow: '#f08a2e', glowOp: 0.28,
          line: '#7c2d12', lineOp: 0.9 }
      : { bloom: '#ff9a3c', halo: '#ffb060', core: '#8a3418',
          bloomOp: 0.26, haloOp: 0.4, coreOp: 1,
          fill: '#e07020', fillOp: 0.1,
          glow: '#ff9a3c', glowOp: 0.32,
          line: '#c45c28', lineOp: 0.92 };
  }

  applyMarkerTheme() {
    const m = this.map, p = this.markerPalette();
    const set = (layer, prop, val) => { if (m.getLayer(layer)) m.setPaintProperty(layer, prop, val); };
    set('hs-bloom', 'circle-color', p.bloom);
    set('hs-halo', 'circle-color', p.halo);
    set('hs-halo', 'circle-opacity', p.haloOp);
    set('hs-core', 'circle-color', p.core);
    set('hs-core', 'circle-opacity', p.coreOp);
    this._bloomOp = p.bloomOp;

    set('hotspot-fill', 'fill-color', p.fill);
    set('hotspot-fill', 'fill-opacity', p.fillOp);
    set('hotspot-glow', 'line-color', p.glow);
    set('hotspot-glow', 'line-opacity', p.glowOp);
    set('hotspot-line', 'line-color', p.line);
    set('hotspot-line', 'line-opacity', p.lineOp);

    // The inventory dots and settlement labels sit on the same ground.
    const light = groundIsLight();
    set('inv-glow', 'circle-color', light ? '#4a6b64' : '#a8cbc7');
    set('inv-point', 'circle-color', light ? '#12241f' : '#e9f2f0');
    set('place-dot', 'circle-color', light ? '#2f5b52' : '#8ebcb6');
    set('place-dot', 'circle-stroke-color', light ? 'rgba(255,255,255,.9)' : 'rgba(0,36,31,.8)');
    if (m.getLayer('place-label')) {
      m.setPaintProperty('place-label', 'text-color', light ? '#1d2f2a' : 'rgba(207,227,224,.72)');
      m.setPaintProperty('place-label', 'text-halo-color',
        light ? 'rgba(255,255,255,.92)' : 'rgba(0,26,22,.85)');
    }

    // Reference geography reads off the theme's own hairline tone.
    const css = getComputedStyle(document.documentElement);
    const line = css.getPropertyValue('--ui-active').trim() || '#8ebcb6';
    for (const id of ['border-country', 'border-admin1', 'graticule-line']) {
      set(id, 'line-color', line);
    }

    set('aoi-line', 'line-color', line);
  }

  /* The pulse writes hs-bloom's opacity on every frame, so anything else that
     wants to animate that property has to stop it first. */
  pausePulse() { this._pulsePaused = true; }
  resumePulse() { this._pulsePaused = false; }

  /* A slow sine on the bloom. ~0.09 Hz: perceptible, never busy. */
  _startPulse() {
    const step = (t) => {
      this.pulse = t;
      if (!this._pulsePaused && this.map.getLayer('hs-bloom')) {
        const k = 1 + 0.08 * Math.sin(t / 1750);
        const ramp = (m) => ['interpolate', ['linear'], ['get', 'risk_score'],
                             0, BLOOM_LO * k * m, 100, BLOOM_HI * k * m];
        this.map.setPaintProperty('hs-bloom', 'circle-radius',
          ['interpolate', ['linear'], ['zoom'],
           7, ramp(0.55), 9, ramp(0.78), 11, ramp(1), 15, ramp(1.7)]);
        const base = this._bloomOp ?? 0.18;
        this.map.setPaintProperty('hs-bloom', 'circle-opacity',
          base - 0.02 + 0.04 * Math.sin(t / 1750));
        if (this.map.getLayer('alert-bloom')) {
          const gain = this._ewNow();
          const pulse = 1 + 0.18 * Math.sin(t / 900);
          this.map.setPaintProperty('alert-bloom', 'circle-radius',
            ['interpolate', ['linear'], ['zoom'],
             7, 10 * pulse, 11, 18 * pulse, 15, 28 * pulse]);
          this.map.setPaintProperty('alert-bloom', 'circle-opacity',
            (0.28 + 0.12 * Math.sin(t / 900)) * gain);
        }
      }
      if (this.map.getLayer('alert-radar-a')) {
        const gain = this._ewNow();
        const cycle = 2400;
        const paintRing = (id, shift) => {
          const p = ((t / cycle) + shift) % 1;
          const radius = 7 + p * 46;
          this.map.setPaintProperty(id, 'circle-radius',
            ['interpolate', ['linear'], ['zoom'], 7, radius * 0.45, 11, radius, 15, radius * 1.5]);
          this.map.setPaintProperty(id, 'circle-stroke-opacity', (1 - p) * 0.85 * gain);
        };
        paintRing('alert-radar-a', 0);
        paintRing('alert-radar-b', 0.5);
      }
      this._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  }

  /* The study-area boundary. Kept so it survives a style rebuild. */
  setOutline(fc) {
    this._outline = fc;
    this.setData('aoi', fc);
  }

  /* Isolate the area of interest. Everything that places Sikkim in its
     surroundings goes: the base-map tiles, the graticule and both border layers.
     What is left is the state's own outline over the theme's ground, with the risk
     surface — which is already clipped to the study area — inside it.

     Hiding the raster rather than masking around it is deliberate. A mask means a
     world-sized polygon with the state punched out of it, and that is a lot of
     geometry to get right for a result you can have by turning one layer off. */
  /* `duration` fades the surroundings out instead of cutting them. The paint
     values are flattened to numbers first: the border opacities are zoom
     expressions, and MapLibre can only interpolate a number. */
  focusArea(on, duration = 0) {
    this._focus = on;
    this._rememberContextPaint();
    if (this._focusTimer) {
      clearTimeout(this._focusTimer);
      this._focusTimer = null;
    }
    if (duration > 0) {
      if (on) this._fadeToFocus(duration);
      else this._fadeFromFocus(duration);
      return;
    }
    this._applyFocus(on);
  }

  _rememberContextPaint() {
    if (this._contextPaint) return;
    const m = this.map;
    const grab = (id, prop) => (m.getLayer(id) ? m.getPaintProperty(id, prop) : undefined);
    this._contextPaint = {
      basemap: grab('basemap', 'raster-opacity'),
      graticule: grab('graticule-line', 'line-opacity'),
      country: grab('border-country', 'line-opacity'),
      admin: grab('border-admin1', 'line-opacity'),
      aoi: grab('aoi-line', 'line-opacity'),
    };
  }

  _applyFocus(on) {
    const m = this.map;
    const vis = (id, v) => { if (m.getLayer(id)) m.setLayoutProperty(id, 'visibility', v); };
    const paint = this._contextPaint || {};
    const restore = (id, prop, value) => {
      if (!m.getLayer(id) || value === undefined) return;
      m.setPaintProperty(id, `${prop}-transition`, { duration: 0, delay: 0 });
      m.setPaintProperty(id, prop, value);
    };
    if (on) {
      vis('aoi-line', 'visible');
      restore('aoi-line', 'line-opacity', paint.aoi ?? 0.9);
      vis('basemap', 'none');
      for (const id of ['graticule-line', 'border-country', 'border-admin1']) vis(id, 'none');
      return;
    }
    restore('basemap', 'raster-opacity', paint.basemap);
    restore('graticule-line', 'line-opacity', paint.graticule);
    restore('border-country', 'line-opacity', paint.country);
    restore('border-admin1', 'line-opacity', paint.admin);
    vis('aoi-line', 'none');
    vis('basemap', 'visible');
    for (const id of ['graticule-line', 'border-country', 'border-admin1']) vis(id, 'visible');
  }

  _fadeToFocus(duration) {
    const m = this.map;
    const vis = (id, v) => { if (m.getLayer(id)) m.setLayoutProperty(id, 'visibility', v); };
    const flatten = (id, prop, fallback) => {
      if (!m.getLayer(id)) return fallback;
      vis(id, 'visible');
      const cur = m.getPaintProperty(id, prop);
      const value = typeof cur === 'number' ? cur : fallback;
      m.setPaintProperty(id, `${prop}-transition`, { duration: 0, delay: 0 });
      m.setPaintProperty(id, prop, value);
      return value;
    };
    this._shownPaint = {
      basemap: flatten('basemap', 'raster-opacity', 1),
      country: flatten('border-country', 'line-opacity', 0.12),
      admin: flatten('border-admin1', 'line-opacity', 0.55),
      graticule: flatten('graticule-line', 'line-opacity', 0),
    };
    if (m.getLayer('aoi-line')) {
      vis('aoi-line', 'visible');
      m.setPaintProperty('aoi-line', 'line-opacity-transition', { duration: 0, delay: 0 });
      m.setPaintProperty('aoi-line', 'line-opacity', 0);
    }
    const fadeDelay = Math.round(duration * 0.22);
    const fadeDuration = duration - fadeDelay;
    requestAnimationFrame(() => {
      const fade = (id, prop, to) => {
        if (!m.getLayer(id)) return;
        m.setPaintProperty(id, `${prop}-transition`, { duration: fadeDuration, delay: fadeDelay });
        m.setPaintProperty(id, prop, to);
      };
      fade('basemap', 'raster-opacity', 0);
      fade('border-country', 'line-opacity', 0);
      fade('border-admin1', 'line-opacity', 0);
      fade('graticule-line', 'line-opacity', 0);
      fade('aoi-line', 'line-opacity', this._contextPaint?.aoi ?? 0.9);
      const t0 = performance.now();
      const tick = (now) => {
        m.triggerRepaint();
        if (now - t0 < duration + 120) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    this._focusTimer = setTimeout(() => {
      this._focusTimer = null;
      if (this._focus) this._applyFocus(true);
    }, duration + 80);
  }

  /* The reverse of the Sikkim-only fade: surroundings come back over the same
     duration, then the stored expressions replace the temporary numbers. */
  _fadeFromFocus(duration) {
    const m = this.map;
    const vis = (id, v) => { if (m.getLayer(id)) m.setLayoutProperty(id, 'visibility', v); };
    const park = (id, prop) => {
      if (!m.getLayer(id)) return;
      const hidden = m.getLayoutProperty(id, 'visibility') === 'none';
      const cur = m.getPaintProperty(id, prop);
      const from = !hidden && typeof cur === 'number' ? cur : 0;
      vis(id, 'visible');
      m.setPaintProperty(id, `${prop}-transition`, { duration: 0, delay: 0 });
      m.setPaintProperty(id, prop, from);
    };
    park('basemap', 'raster-opacity');
    park('border-country', 'line-opacity');
    park('border-admin1', 'line-opacity');
    park('graticule-line', 'line-opacity');
    const shown = this._shownPaint || { basemap: 1, country: 0.5, admin: 0.5, graticule: 0 };
    const fadeDelay = Math.round(duration * 0.22);
    const fadeDuration = Math.max(0, duration - fadeDelay);
    requestAnimationFrame(() => {
      const fade = (id, prop, to) => {
        if (!m.getLayer(id)) return;
        m.setPaintProperty(id, `${prop}-transition`, { duration: fadeDuration, delay: fadeDelay });
        m.setPaintProperty(id, prop, to);
      };
      fade('basemap', 'raster-opacity', shown.basemap ?? 1);
      fade('border-country', 'line-opacity', shown.country ?? 0.5);
      fade('border-admin1', 'line-opacity', shown.admin ?? 0.5);
      fade('graticule-line', 'line-opacity', shown.graticule ?? 0);
      fade('aoi-line', 'line-opacity', 0);
      const t0 = performance.now();
      const tick = (now) => {
        m.triggerRepaint();
        if (now - t0 < duration + 120) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
    this._focusTimer = setTimeout(() => {
      this._focusTimer = null;
      if (!this._focus) this._applyFocus(false);
    }, duration + 80);
  }

  /* Hold the base map back whenever the ground is meant to be dark. */
  static sinksBasemap() {
    return !groundIsLight();
  }

  /* The style is built from the live palette, so a theme swap needs no table here. */
  static styleOpts() {
    return {
      dark: MapView.sinksBasemap(),
      ground: getComputedStyle(document.documentElement).getPropertyValue('--ground').trim(),
    };
  }

  /* Rebuild the style, then put back everything that lives above it. The base-map
     exposure is part of the style rather than a later paint call, so this is the
     only way to change it — see the note in basemaps.js. */
  async reloadStyle(restore) {
    this.clearLiveRain();
    this._contextPaint = null;
    if (this._focusTimer) {
      clearTimeout(this._focusTimer);
      this._focusTimer = null;
    }
    this.map.setStyle(baseStyle(this.basemap, MapView.styleOpts()));
    await new Promise(res => this.map.once('styledata', res));
    this._scaffold();
    this.applyMarkerTheme();
    if (this._outline) this.setOutline(this._outline);
    this.focusArea(!!this._focus);
    restore?.();
  }

  async setBasemap(id, restore) {
    const bm = BASEMAPS.find(b => b.id === id);
    if (!bm || bm.id === this.basemap.id) return;
    this.basemap = bm;
    await this.reloadStyle(restore);
  }

  addRaster(id, opacity) {
    if (this.map.getSource(`r-${id}`)) return;
    const [w, s, e, n] = this.meta.grid.bounds_wgs84;
    const pad = 0.02;
    this.map.addSource(`r-${id}`, {
      type: 'raster', tiles: [api.tileUrl(id)], tileSize: this.meta.tile_size || 256,
      minzoom: 0, maxzoom: this.meta.max_zoom || 16,
      // Only tiles that meet Sikkim. A globe flight must not spend the
      // worker rendering tiles over the ocean while the risk map is waiting.
      bounds: [w - pad, s - pad, e + pad, n + pad],
    });
    this.map.addLayer({
      id: `r-${id}`, type: 'raster', source: `r-${id}`,
      paint: { 'raster-opacity': opacity, 'raster-fade-duration': 260,
               'raster-resampling': 'nearest',
               'raster-opacity-transition': { duration: 420 } },
    }, RASTER_ROOT);
    this.rasterOrder.push(id);
  }

  removeRaster(id) {
    if (this.map.getLayer(`r-${id}`)) this.map.removeLayer(`r-${id}`);
    if (this.map.getSource(`r-${id}`)) this.map.removeSource(`r-${id}`);
    this.rasterOrder = this.rasterOrder.filter(x => x !== id);
  }

  setRasterOpacity(id, o) {
    if (this.map.getLayer(`r-${id}`)) this.map.setPaintProperty(`r-${id}`, 'raster-opacity', o);
  }

  setData(source, data) { this.map.getSource(source)?.setData(data); }

  /* The 5-class risk surface, fetched before any other tile so it is the
     first thing on the map. Zooms 5–7 cover the state in a handful of tiles,
     which is the view the opening flight lands on. */
  prefetchRisk() {
    const seen = this._warmed || (this._warmed = new Set());
    const src = this.map.getSource('r-lsm_class');
    const template = (src && src.tiles && src.tiles[0]) || api.tileUrl('lsm_class');
    const [w, s, e, n] = this.meta.grid.bounds_wgs84;
    const here = Math.round(this.map.getZoom());
    const zooms = [...new Set([5, 6, 7, here].filter(z => z >= 1 && z <= 11))];
    const urls = [];
    for (const z of zooms) {
      const key = `lsm_class@${z}`;
      if (seen.has(key)) continue;
      const cover = this._tilesCovering(w, s, e, n, z);
      if (cover.length > 8) continue;
      seen.add(key);
      for (const [x, y] of cover) {
        urls.push(template.replace('{z}', String(z)).replace('{x}', String(x)).replace('{y}', String(y)));
      }
    }
    this.riskReady = this._fetchTiles(urls, { priority: 'high' });
    return this.riskReady;
  }

  /* Fetch the tiles for this view ahead of a toggle, one layer at a time.
     MapLibre then finds them in the browser cache instead of waiting on the
     server the moment the box is checked. */
  warmTiles(layerIds) {
    const seen = this._warmed || (this._warmed = new Set());
    let queue = Promise.resolve();
    const schedule = () => {
      const z = Math.round(this.map.getZoom());
      if (z < 6 || z > 12) return;
      const b = this.map.getBounds();
      const cover = this._tilesCovering(b.getWest(), b.getSouth(), b.getEast(), b.getNorth(), z);
      if (cover.length > 12) return;
      for (const id of layerIds) {
        if (id === 'lsm_class') continue;
        if (this.map.getSource(`r-${id}`)) continue;
        const key = `${id}@${z}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const urls = cover.map(([x, y]) =>
          api.tileUrl(id).replace('{z}', String(z)).replace('{x}', String(x)).replace('{y}', String(y)));
        queue = queue.then(() => this._fetchTiles(urls));
      }
    };
    this.map.on('idle', schedule);
    schedule();
  }

  _tilesCovering(west, south, east, north, z) {
    const n = 2 ** z;
    const xOf = (lon) => Math.floor((lon + 180) / 360 * n);
    const yOf = (lat) => {
      const r = lat * Math.PI / 180;
      return Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * n);
    };
    const x0 = xOf(west);
    const x1 = xOf(east);
    const y0 = yOf(north);
    const y1 = yOf(south);
    const tiles = [];
    for (let x = x0; x <= x1; x++) {
      for (let y = y0; y <= y1; y++) tiles.push([x, y]);
    }
    return tiles;
  }

  _fetchTiles(urls, opts) {
    const take = urls.splice(0, 3);
    if (!take.length) return Promise.resolve();
    return Promise.all(take.map(url => fetch(url, opts).catch(() => null)))
      .then(() => this._fetchTiles(urls, opts));
  }

  setVisible(layerIds, on) {
    for (const id of layerIds) {
      if (this.map.getLayer(id)) {
        this.map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none');
      }
    }
  }

  /* Early-warning layers arrive and leave with the same ease-out the drawer
     uses. The radar rings are repainted every frame, so their strength follows
     a gain instead of a paint transition the pulse would cancel. */
  fadeLayers(ids, on) {
    const m = this.map;
    const show = !!on;
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const ms = reduce ? 0 : 550;
    if (ids.some(id => id.startsWith('alert-'))) this._aimEw(show ? 1 : 0);
    if (!this._fadeToken) this._fadeToken = {};
    const token = {};
    const pulsed = new Set(['alert-bloom', 'alert-radar-a', 'alert-radar-b']);
    for (const id of ids) {
      if (!m.getLayer(id)) continue;
      this._fadeToken[id] = token;
      if (pulsed.has(id)) {
        if (show) m.setLayoutProperty(id, 'visibility', 'visible');
        continue;
      }
      const visible = m.getLayoutProperty(id, 'visibility') !== 'none';
      if (show && visible) continue;
      const props = this._fadeProps(id);
      if (show) {
        for (const prop of props) this._baseOp(id, prop);
        m.setLayoutProperty(id, 'visibility', 'visible');
        if (ms === 0) {
          for (const prop of props) {
            m.setPaintProperty(id, `${prop}-transition`, { duration: 0, delay: 0 });
            m.setPaintProperty(id, prop, this._baseOp(id, prop));
          }
        } else {
          for (const prop of props) {
            m.setPaintProperty(id, `${prop}-transition`, { duration: 0, delay: 0 });
            m.setPaintProperty(id, prop, 0);
          }
        }
      } else if (visible) {
        for (const prop of props) this._baseOp(id, prop);
        if (ms === 0) {
          m.setLayoutProperty(id, 'visibility', 'none');
        } else {
          for (const prop of props) {
            m.setPaintProperty(id, `${prop}-transition`, { duration: ms, delay: 0 });
            m.setPaintProperty(id, prop, 0);
          }
        }
      }
    }
    if (ms > 0) {
      const t0 = performance.now();
      const tick = (now) => {
        m.triggerRepaint();
        if (now - t0 < ms + 80) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(() => {
      if (!show || ms === 0) return;
      for (const id of ids) {
        if (this._fadeToken[id] !== token || !m.getLayer(id) || pulsed.has(id)) continue;
        for (const prop of this._fadeProps(id)) {
          m.setPaintProperty(id, `${prop}-transition`, { duration: ms, delay: 0 });
          m.setPaintProperty(id, prop, this._baseOp(id, prop));
        }
      }
    });
    setTimeout(() => {
      if (show) return;
      for (const id of ids) {
        if (this._fadeToken[id] !== token || !m.getLayer(id)) continue;
        m.setLayoutProperty(id, 'visibility', 'none');
        if (pulsed.has(id)) continue;
        for (const prop of this._fadeProps(id)) {
          m.setPaintProperty(id, `${prop}-transition`, { duration: 0, delay: 0 });
          m.setPaintProperty(id, prop, this._baseOp(id, prop));
        }
      }
    }, ms + 60);
  }

  _aimEw(to) {
    this._ewFrom = this._ew ?? 0;
    this._ewTarget = to;
    this._ewT0 = performance.now();
  }

  _ewNow() {
    if (this._ewT0 == null) return this._ew ?? 0;
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    const p = reduce ? 1 : Math.min(1, (performance.now() - this._ewT0) / 550);
    const e = 1 - Math.pow(1 - p, 3);
    this._ew = this._ewFrom + (this._ewTarget - this._ewFrom) * e;
    if (p >= 1) this._ewT0 = null;
    return this._ew;
  }

  _fadeProps(id) {
    const type = this.map.getLayer(id)?.type;
    if (type === 'line') return ['line-opacity'];
    if (type === 'symbol') return ['text-opacity'];
    if (type === 'circle') return ['circle-opacity', 'circle-stroke-opacity'];
    return [];
  }

  _baseOp(id, prop) {
    if (!this._op) this._op = new Map();
    const key = `${id}\0${prop}`;
    if (!this._op.has(key)) {
      const v = this.map.getPaintProperty(id, prop);
      this._op.set(key, typeof v === 'number' ? v : 1);
    }
    return this._op.get(key);
  }

  /* Filter the luminous points without refetching — the rail uses this. */
  setHotspotFilter(expr) {
    for (const id of ['hs-bloom', 'hs-halo', 'hs-core', 'hs-hit']) {
      if (this.map.getLayer(id)) this.map.setFilter(id, expr);
    }
  }

  highlight(geometry) {
    this.setData('sel', geometry
      ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry }] }
      : { type: 'FeatureCollection', features: [] });
  }

  flyTo(lon, lat, zoom = 13) {
    this.map.flyTo({ center: [lon, lat], zoom, speed: 1.1, curve: 1.5, essential: true });
  }

  fitBounds(bbox, padding = 90) {
    this.map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding, maxZoom: 14, speed: 1.1 });
  }

  /* Keep Sikkim clear of the chrome.

     The rail sits to the right of the layers drawer, so with the drawer open
     there is roughly 500 px of furniture down the left edge and the readout down
     the right. MapLibre's camera padding shifts the centre for us, which is
     cheaper and smoother than re-fitting bounds on every toggle. */
  /* Width of the layers drawer when it is open, 0 when it is not. */
  sidebarWidth() {
    if (document.body.classList.contains('sidebar-closed')) return 0;
    return parseInt(getComputedStyle(document.documentElement)
      .getPropertyValue('--sidebar-w'), 10) || 326;
  }

  chromePadding(sidebarOpen) {
    if (window.innerWidth < 900) return { top: 20, bottom: 92, left: 14, right: 14 };
    const railRoom = 62;
    const sidebarW = parseInt(
      getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'), 10) || 326;
    return {
      top: 56, bottom: 84,
      left: (sidebarOpen ? sidebarW : 0) + railRoom,
      right: 250,
    };
  }

  applyChromePadding(sidebarOpen, animate = true) {
    const pad = this.chromePadding(sidebarOpen);
    if (animate) this.map.easeTo({ padding: pad, duration: 520 });
    else this.map.setPadding(pad);
  }

  /* Place Sikkim in the space the chrome actually leaves.

     setPadding alone stores the padding for later camera moves but does not
     re-place the current view, and the constructor's fitBounds ran before the
     drawer's restored state was known. Re-fitting against the same padding is
     what actually centres it. */
  refit(sidebarOpen, animate = false) {
    const [w, s, e, n] = this.meta.grid.bounds_wgs84;
    this.map.fitBounds([[w, s], [e, n]], {
      padding: this.chromePadding(sidebarOpen),
      duration: animate ? 620 : 0,
    });
  }

  bboxString() {
    const b = this.map.getBounds();
    return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(v => v.toFixed(4)).join(',');
  }

  /* Observed rainfall, one raster per catalog frame. The visible frame steps
     every 0.8 s so the last two hours play, then the short nowcast, then it
     loops. Tiles come straight from LibreWXR; only the frame list is proxied. */
  setLiveRain(on) {
    this._liveGen = (this._liveGen || 0) + 1;
    const gen = this._liveGen;
    this.clearLiveRain();
    if (!on) return;
    const pull = (left) => {
      api.liveRain().then(cat => {
        if (gen !== this._liveGen) return;
        this._armLiveRain(cat);
      }).catch(() => {
        if (gen !== this._liveGen) return;
        if (left > 0) {
          setTimeout(() => pull(left - 1), 1200);
          return;
        }
        const clock = document.getElementById('live-rain-when');
        if (clock) clock.textContent = 'feed down';
        const box = document.getElementById('ly-live-rain');
        if (box) {
          box.checked = false;
          box.closest('.layer')?.classList.remove('is-on');
        }
      });
    };
    pull(1);
  }

  restoreLiveRain() {
    if (document.getElementById('ly-live-rain')?.checked) this.setLiveRain(true);
  }

  setLiveRainOpacity(opacity) {
    this._liveOpacity = opacity;
    const id = this._liveIds?.[this._liveIndex];
    if (id && this.map.getLayer(id)) this.map.setPaintProperty(id, 'raster-opacity', opacity);
  }

  clearLiveRain() {
    clearInterval(this._liveTimer);
    this._liveTimer = null;
    for (const id of this._liveIds || []) {
      if (this.map.getLayer(id)) this.map.removeLayer(id);
      if (this.map.getSource(id)) this.map.removeSource(id);
    }
    this._liveIds = [];
    this._liveFrames = null;
  }

  _armLiveRain(cat) {
    const frames = cat.frames || [];
    if (!frames.length) return;
    this._liveFrames = frames;
    this._liveStale = Boolean(cat.stale);
    this._liveOpacity = this._liveOpacity ?? 0.8;
    this._liveIds = frames.map((f, i) => {
      const id = `live-rain-${i}`;
      this.map.addSource(id, {
        type: 'raster',
        tiles: [`${cat.host}${f.path}/256/{z}/{x}/{y}/2/1_1.png`],
        tileSize: 256,
        maxzoom: cat.max_zoom || 6,
      });
      this.map.addLayer({
        id, type: 'raster', source: id,
        paint: { 'raster-opacity': 0, 'raster-fade-duration': 0, 'raster-resampling': 'linear' },
      }, RASTER_ROOT);
      return id;
    });
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const start = reduce
      ? Math.max(0, frames.findLastIndex(f => f.kind === 'past'))
      : 0;
    this._showLiveFrame(start);
    if (!reduce && frames.length > 1) {
      this._liveTimer = setInterval(() => {
        this._showLiveFrame((this._liveIndex + 1) % frames.length);
      }, 800);
    }
  }

  _showLiveFrame(i) {
    const prev = this._liveIds?.[this._liveIndex];
    if (prev && this.map.getLayer(prev)) this.map.setPaintProperty(prev, 'raster-opacity', 0);
    this._liveIndex = i;
    const id = this._liveIds?.[i];
    if (id && this.map.getLayer(id)) {
      this.map.setPaintProperty(id, 'raster-opacity', this._liveOpacity ?? 0.8);
    }
    const frame = this._liveFrames?.[i];
    if (!frame) return;
    const label = this._frameLabel(frame);
    const clock = document.getElementById('live-rain-when');
    if (clock) clock.textContent = label;
    const leg = document.querySelector('.legend-live-when');
    if (leg) leg.textContent = label;
  }

  _frameLabel(frame) {
    const text = new Intl.DateTimeFormat('en-GB', {
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'Asia/Kolkata',
    }).format(new Date(frame.time * 1000));
    const bits = [text];
    if (frame.kind === 'nowcast') bits.push('forecast');
    if (this._liveStale) bits.push('saved');
    return bits.join(' ');
  }

  liveRainLegend() {
    if (!document.getElementById('ly-live-rain')?.checked) return '';
    const when = document.getElementById('live-rain-when')?.textContent || '';
    return `<div class="legend">
      <div class="legend-title">Live Rain Radar<span class="legend-live-when">${when}</span></div>
      <div class="legend-ramp" style="background:linear-gradient(90deg,#d4f4ff 0%,#3d8ec8 52%,#f2e34a 100%)"></div>
      <div class="legend-scale"><span>light</span><span>heavy</span></div>
    </div>`;
  }
}
