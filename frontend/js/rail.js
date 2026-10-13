/* The risk-class rail — this project's answer to Statskog's year timeline.

   Statskog steps through award years; we step through the five susceptibility
   categories. Same interaction, same receding-perspective treatment: the active
   item is large and white, and its neighbours fall away in size and opacity with
   distance, so the list reads as depth rather than as a menu.

   Selecting a class filters both the raster (via the tile layer's own class
   colouring) and the luminous hotspot points. */
import { esc, fmt } from './api.js';

const CLASSES = [
  { id: 'all', label: 'All', cls: null },
  { id: 'c5', label: 'Very High', cls: 5 },
  { id: 'c4', label: 'High', cls: 4 },
  { id: 'c3', label: 'Moderate', cls: 3 },
  { id: 'c2', label: 'Low', cls: 2 },
  { id: 'c1', label: 'Very Low', cls: 1 },
];

/* Size and opacity by distance from the active item — the depth effect. */
const SIZE = [30, 24, 19.5, 16, 13.5, 11.5, 10];
const FADE = [1, 0.62, 0.42, 0.29, 0.2, 0.14, 0.1];

export class ClassRail {
  constructor(el, stats, onChange) {
    this.el = el;
    this.onChange = onChange;
    this.active = 0;
    this.byClass = {};
    for (const row of stats?.area?.by_class || []) this.byClass[row.class] = row;
    this._render();
  }

  _render() {
    this.el.innerHTML = CLASSES.map((c, i) => {
      const row = c.cls ? this.byClass[c.cls] : null;
      const sub = row ? `${row.share_pct}%` : '';
      return `<button class="rail-item" data-i="${i}" data-cls="${c.cls ?? ''}">
        ${esc(c.label)}${sub ? `<span class="rail-count">${sub}</span>` : ''}
      </button>`;
    }).join('');

    this.items = [...this.el.querySelectorAll('.rail-item')];
    this.items.forEach(b => b.addEventListener('click', () => this.select(Number(b.dataset.i))));
    this._paint();
  }

  _paint() {
    this.items.forEach((b, i) => {
      const d = Math.abs(i - this.active);
      b.style.fontSize = `${SIZE[Math.min(d, SIZE.length - 1)]}px`;
      b.style.opacity = FADE[Math.min(d, FADE.length - 1)];
      b.classList.toggle('is-active', i === this.active);
    });
  }

  select(i) {
    if (i === this.active) return;
    this.active = i;
    this._paint();
    this.onChange(CLASSES[i]);
  }

  get current() { return CLASSES[this.active]; }
}

/* ------------------------------------------------------------------------- */
/* The big serif readout on the right. Numbers count up rather than snap —
   the movement is what makes a filter change feel like a change.             */
export class Readout {
  constructor(el) { this.el = el; this.timers = []; }

  show(blocks) {
    this.timers.forEach(clearInterval);
    this.timers = [];
    this.el.innerHTML = blocks.map((b, i) => `
      <div class="readout-block" style="animation-delay:${i * 90}ms">
        <div class="readout-label">${esc(b.label)}</div>
        <div class="rule-node"><i></i></div>
        <div class="readout-value" data-to="${b.value}" data-dec="${b.decimals || 0}">0${
          b.unit ? `<small>${esc(b.unit)}</small>` : ''}</div>
        ${b.note ? `<div class="readout-note">${esc(b.note)}</div>` : ''}
      </div>`).join('');

    this.el.querySelectorAll('.readout-value').forEach(node => this._countUp(node));
  }

  _countUp(node) {
    const to = Number(node.dataset.to) || 0;
    const dec = Number(node.dataset.dec) || 0;
    const small = node.querySelector('small');
    const unit = small ? small.outerHTML : '';
    const t0 = performance.now();
    const dur = 900;
    const tick = (t) => {
      // Clamped at both ends: the rAF timestamp and performance.now() are not
      // guaranteed to share an origin, and a negative p sends easeOutCubic to a
      // large negative number — the readout briefly shows a nonsense figure.
      const p = Math.min(1, Math.max(0, (t - t0) / dur));
      const eased = 1 - Math.pow(1 - p, 3);          // easeOutCubic
      node.innerHTML = fmt.num(to * eased, dec) + unit;
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
}
