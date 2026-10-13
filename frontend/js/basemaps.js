/* Base maps.

   Two, deliberately: a pale canvas and a topographic one. The review settled on a
   light presentation, and every extra swatch was a chance to demo the product on a
   ground that fights the risk ramp — Satellite and Dark both did exactly that, and
   the tile-free "Plain" ground is now the light theme's own vignette anyway.

   Neither style paints an opaque background colour. That is load-bearing rather
   than decorative: with the background transparent the CSS vignette shows through
   the canvas, so if the tile CDN is slow — or demo wifi is gone entirely — the map
   still draws the risk surface over a coloured ground instead of a blank rectangle.

   Both sources are key-free. CARTO is not used and should not be re-added: its
   basemap CDN now stamps "API KEY REQUIRED" across every tile.

   Under the light theme the tiles are drawn exactly as the source serves them. The
   page was moved to the map rather than the reverse — the light --ground is #efefef,
   the measured land colour of Esri's canvas — so there is nothing to correct.

   The dark themes cannot do that: both sources are pale, and a white slab under a
   dark UI is not just ugly. Every piece of map furniture there is light type with a
   dark halo, so over a white ground the rail, the readout and the legend all vanish.
   So a dark theme sinks the tiles with `raster-brightness-max`, which leaves them as
   faint reference geography on a ground the luminous markers were drawn for.

   The exposure is baked into the STYLE, not pushed later with setPaintProperty.
   That is not a stylistic preference: the identical value applied after load did
   not repaint the already-rasterised tiles, while the same value in the style
   always renders. Changing theme therefore rebuilds the style — see
   MapView.reloadStyle().

   Glyphs are served from our own origin. That is not a nicety: MapLibre parses
   glyph responses as protobuf, and a public glyph host that answers with an HTML
   error page throws inside the tile worker and takes the whole source's parse down
   with it — the symbol layer AND every circle layer sharing that source vanish. */
export const GLYPHS = '/fonts/{fontstack}/{range}.pbf';

const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';

export const BASEMAPS = [
  {
    id: 'light', label: 'Light',
    tiles: [`${ESRI}/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`],
    attribution: 'Esri, HERE, Garmin, © OpenStreetMap contributors', maxzoom: 16,
    swatch: 'linear-gradient(135deg,#f6f6f6,#dcdcdc)',
  },
  {
    id: 'terrain', label: 'Terrain',
    tiles: ['https://a.tile.opentopomap.org/{z}/{x}/{y}.png',
            'https://b.tile.opentopomap.org/{z}/{x}/{y}.png',
            'https://c.tile.opentopomap.org/{z}/{x}/{y}.png'],
    attribution: '© OpenTopoMap (CC-BY-SA), © OpenStreetMap contributors', maxzoom: 17,
    swatch: 'linear-gradient(135deg,#e8e0d0,#b9c9a6)',
  },
];

/* How far a theme holds the base map back. Light shows the tiles as served; a dark
   theme dims them AND lets them go part-transparent over its own ground colour, so
   the map carries the theme's hue instead of going a generic grey. */
const EXPOSURE = {
  light: { 'raster-opacity': 1, 'raster-brightness-max': 1, 'raster-saturation': 0 },
  dark: { 'raster-opacity': 0.5, 'raster-brightness-max': 0.5, 'raster-saturation': -0.3 },
};

export function baseStyle(bm, { dark = false, ground = null } = {}) {
  const sources = {};
  /* Transparent on the light theme so the CSS vignette shows through the canvas.
     A dark theme paints its own ground here instead: the tiles sit on it at half
     opacity, which is what tints them, and it is also the colour of space behind
     the globe during the opening flight. */
  const layers = [{
    id: 'bg', type: 'background',
    paint: { 'background-color': dark && ground ? ground : 'rgba(0,0,0,0)' },
  }];

  if (bm.tiles) {
    sources.basemap = {
      type: 'raster', tiles: bm.tiles, tileSize: 256,
      attribution: bm.attribution, maxzoom: bm.maxzoom || 19,
    };
    layers.push({
      id: 'basemap', type: 'raster', source: 'basemap',
      paint: {
        'raster-fade-duration': 220,
        ...(dark ? EXPOSURE.dark : EXPOSURE.light),
      },
    });
  }

  return {
    version: 8,
    glyphs: GLYPHS,
    // MapLibre 5's globe projection transitions to mercator on its own as you
    // zoom in, so one setting covers both the planet view and the flat map.
    projection: { type: 'globe' },
    sources,
    layers,
  };
}
