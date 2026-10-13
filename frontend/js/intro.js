/* The opening flight: a globe that turns, descends onto Sikkim, and hands over
   to the map as the data fades up under it.

   Two things make it read as a shot rather than a transition:

     - the turn and the descent are a single flyTo. MapLibre interpolates pan and
       zoom together along van Wijk's smooth-zoom path, so the planet is still
       rotating as it grows — the Google Earth behaviour. Rotating first and then
       zooming reads as two separate camera operations, because it is;
     - the layers arrive after the camera has stopped, staggered, so the eye
       lands on terrain first and the data resolves onto it.

   It is skippable on any input, and it does not run at all for anyone who has
   asked their system for reduced motion. */

// Longitude chosen so the shorter way round runs EAST. 88.47 - (-75) = 163.5°,
// just under the 180° flip, so the camera travels Atlantic -> Africa -> Arabia ->
// India rather than backwards across the Pacific.
const START = { lon: -75, lat: 18, zoom: 1.45 };

/* Layers held back during the flight, with the paint property that fades them
   and the order they arrive in. */
const CURTAIN = [
  { id: 'r-lsm_class', prop: 'raster-opacity', delay: 0 },
  { id: 'hs-bloom', prop: 'circle-opacity', delay: 500 },
  { id: 'hs-halo', prop: 'circle-opacity', delay: 500 },
  { id: 'hs-core', prop: 'circle-opacity', delay: 500 },
  // Settlements last, and only once the surface underneath them has finished
  // arriving — place names over a still-fading raster look like a mistake.
  { id: 'place-dot', prop: 'circle-opacity', delay: 1250 },
  { id: 'place-label', prop: 'text-opacity', delay: 1250 },
];

export function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
}

const wait = (ms) => new Promise(r => setTimeout(r, ms));

function flyTo(map, opts, signal) {
  return new Promise(resolve => {
    if (signal.cancelled) return resolve();
    const done = () => { map.off('moveend', done); resolve(); };
    map.on('moveend', done);
    map.flyTo({ ...opts, essential: true });
  });
}

export class Intro {
  constructor(view) {
    this.view = view;
    this.map = view.map;
    this.signal = { cancelled: false };
    this.targets = new Map();
  }

  /* Record each layer's intended opacity, then hold it at zero. Called before
     the boot screen lifts, so nothing is ever seen at full strength first. */
  lower() {
    const m = this.map;
    for (const { id, prop } of CURTAIN) {
      if (!m.getLayer(id)) continue;
      this.targets.set(id, m.getPaintProperty(id, prop));
      m.setPaintProperty(id, `${prop}-transition`, { duration: 0 });
      m.setPaintProperty(id, prop, 0);
    }
    // The pulse writes hs-bloom's opacity every frame and would fight the fade.
    this.view.pausePulse();
    document.body.classList.add('is-intro');
    // Centre the globe in the space the drawer leaves. The rail and readout are
    // held back during the flight, so reserving room for them here would push the
    // planet off to one side of its own opening shot.
    this.map.jumpTo({
      center: [START.lon, START.lat],
      zoom: START.zoom,
      padding: { top: 0, bottom: 0, left: this.view.sidebarWidth(), right: 0 },
    });
  }

  /* Put everything back exactly as it was, whether we finished or were skipped. */
  raise(instant = false) {
    const m = this.map;
    for (const { id, prop, delay } of CURTAIN) {
      if (!m.getLayer(id) || !this.targets.has(id)) continue;
      const target = this.targets.get(id);
      m.setPaintProperty(id, `${prop}-transition`,
        instant ? { duration: 0 } : { duration: 900, delay });
      m.setPaintProperty(id, prop, target);
    }
    this.view.resumePulse();
    document.body.classList.remove('is-intro');

    // Keep the render loop alive across the staged fades.
    //
    // MapLibre drives transitions from its render loop and stops rendering once
    // the map goes idle. The map IS idle here — the camera has stopped and every
    // tile is in — so the delayed fades (settlements are 1250 ms out) can land
    // with nothing scheduled to paint them. On desktop something else usually
    // ticks the loop and hides this; on a phone the raster simply never appeared
    // until the next interaction forced a repaint.
    if (!instant) this._keepPainting(2600);
  }

  _keepPainting(ms) {
    const t0 = performance.now();
    const tick = (now) => {
      this.map.triggerRepaint();
      if (now - t0 < ms) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  cancel() {
    if (this.signal.cancelled) return;
    this.signal.cancelled = true;
    this.map.stop();
    this.view.refit(!document.body.classList.contains('sidebar-closed'), false);
    this.raise(true);
  }

  async play() {
    const s = this.signal;
    const [w, so, e, n] = this.view.meta.grid.bounds_wgs84;
    const padding = this.view.chromePadding(
      !document.body.classList.contains('sidebar-closed'));

    // Ask the map where it would put the camera to frame Sikkim in THIS viewport,
    // rather than flying to a zoom measured once on a desktop. 8.98 fits a 1680px
    // window and overshoots a phone by most of a zoom level, cropping the state.
    const target = this.map.cameraForBounds([[w, so], [e, n]], { padding });
    const centre = target ? target.center : [(w + e) / 2, (so + n) / 2];
    const zoom = target ? target.zoom : 8.98;

    // ONE move, not two. flyTo interpolates the turn and the descent together —
    // van Wijk's smooth-zoom path — so the globe is still rotating as it grows,
    // which is the Google Earth behaviour. Rotating first and then zooming reads
    // as two separate camera operations, because it is.
    //
    // See START: the start longitude picks the direction of travel.
    await flyTo(this.map, {
      center: centre,
      zoom,
      duration: 7600,
      curve: 1.25,
      // Ease in AND out. A pure ease-out covered almost the whole rotation in
      // the first second and then spent six more just zooming, which is the very
      // split this is meant to avoid; symmetric easing spreads pan and zoom
      // evenly across the shot.
      easing: (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
      padding,
    }, s);
    if (s.cancelled) return;

    // Do not reveal anything until the tiles for THIS view are actually decoded.
    // The layers are in the style throughout the flight, so MapLibre has been
    // fetching them the whole way down; this only guards the last moment, where
    // fading in a half-loaded raster would show it assembling itself.
    await this.settle(2500);
    if (s.cancelled) return;

    // The data resolves onto the terrain the camera has just settled on.
    this.raise();
    await wait(500);
    document.body.classList.remove('is-intro');
  }

  /* Resolve once every tile in view has loaded, or after `capMs` regardless —
     a slow connection should delay the reveal, not cancel it. */
  settle(capMs) {
    const m = this.map;
    if (m.areTilesLoaded()) return Promise.resolve();
    return new Promise(resolve => {
      const done = () => { clearTimeout(timer); m.off('idle', done); resolve(); };
      const timer = setTimeout(done, capMs);
      m.on('idle', done);
    });
  }
}

/* Run the flight, wiring the escape hatches. Takes the instance that already
   lowered the curtain — the curtain has to drop before the boot screen lifts,
   which happens earlier than this. Resolves once the map is in its final state
   either way. */
export async function runIntro(intro) {
  if (prefersReducedMotion()) {
    intro.raise(true);
    return;
  }

  const skip = () => intro.cancel();
  const events = ['pointerdown', 'wheel', 'keydown'];
  events.forEach(e => window.addEventListener(e, skip, { once: true, passive: true }));

  try {
    await intro.play();
  } finally {
    events.forEach(e => window.removeEventListener(e, skip));
  }
}
