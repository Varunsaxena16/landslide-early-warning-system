/* Vertical carousel on the readout. One hazard at a time, stepping upward.

   The items are already filtered to Sikkim and the neighbouring hills.
   A card with coordinates flies the map there. A headline without a point
   opens the source. Reduced motion holds the first item still. */
import { esc } from './api.js';

const KIND = {
  earthquake: 'Earthquake',
  landslide: 'Landslide',
  flood: 'Flood',
  rainfall: 'Heavy rain',
  cyclone: 'Cyclone',
  news: 'Report',
  info: 'Neighbourhood',
};

const LEVEL = { critical: 'Critical', watch: 'Watch', info: 'Note' };

/* Small stroke icons, one per hazard. currentColor takes the tag colour. */
const ICON = {
  earthquake: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1 8.5h2.2L5 3.5l2.2 9 2.1-6.2L11 9h4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>',
  landslide: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 12.5 8 3.5l6.5 9" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M5.5 10.5 8 13l2.2-3.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  flood: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1 6.2c1.4 1.1 2.4 1.1 3.8 0s2.4-1.1 3.8 0 2.4 1.1 3.8 0 2.2-1.1 3.6 0M1 10.2c1.4 1.1 2.4 1.1 3.8 0s2.4-1.1 3.8 0 2.4 1.1 3.8 0 2.2-1.1 3.6 0" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
  rainfall: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.2 7.2a3 3 0 0 1 5.2-2 2.4 2.4 0 0 1 3.6 2.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M5 11.2v1.6M8 10.2v2.4M11 11.2v1.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
  cyclone: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 8.2c1.8 0 2.8-.8 2.8-1.8S9.4 5 8 6s-1.6 1.8-.6 2.8 2.6.8 3.6-.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
  news: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2.8h7.2v10.4H3zM10.2 5.6H13v7.2H6.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
  info: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.2" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 7.2V11M8 5.2h.01" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
};

const STEP_MS = 4200;

function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!Number.isFinite(s) || s < 0) return '';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

export class Bulletin {
  constructor(el, onPick) {
    this.el = el;
    this.onPick = onPick;
    this.items = [];
    this.index = 0;
    this.timer = null;
    this.reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    this.track = el.querySelector('.bulletin-track');
    this.window = el.querySelector('.bulletin-window');
    this.el.addEventListener('mouseenter', () => this.pause());
    this.el.addEventListener('mouseleave', () => this.play());
    this.el.addEventListener('focusin', () => this.pause());
    this.el.addEventListener('focusout', () => this.play());
  }

  set(doc) {
    const items = (doc?.items || []).filter(it => it && it.title);
    this.items = items;
    this.index = 0;
    this.pause();
    if (!items.length) {
      this.track.innerHTML = '';
      return;
    }
    const slides = this.reduce || items.length < 2 ? items : [...items, items[0]];
    this.track.innerHTML = slides.map(it => this._card(it)).join('');
    this.track.querySelectorAll('.bulletin-card').forEach((node, i) => {
      const item = slides[i];
      node.addEventListener('click', () => this.onPick(item));
    });
    this.track.style.transition = 'none';
    this.track.style.transform = 'translateY(0)';
    this.play();
  }

  _card(it) {
    const key = KIND[it.kind] ? it.kind : 'news';
    const kind = KIND[key];
    const level = LEVEL[it.level] || '';
    const when = ago(it.when);
    const meta = [it.place, it.source, when].filter(Boolean).join(' · ');
    return `<button type="button" class="bulletin-card is-${esc(key)} is-${esc(it.level || 'info')}">
      <span class="bulletin-head">
        <span class="bulletin-tag">${ICON[key] || ICON.news}${esc(kind)}</span>
        ${level ? `<span class="bulletin-level">${esc(level)}</span>` : ''}
      </span>
      <span class="bulletin-title">${esc(it.title)}</span>
      <span class="bulletin-meta">${esc(meta)}</span>
    </button>`;
  }

  play() {
    this.pause();
    if (this.reduce || this.items.length < 2 || this.el.matches(':hover')) return;
    this.timer = setInterval(() => this._step(), STEP_MS);
  }

  pause() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  _step() {
    const h = this.window.clientHeight;
    if (!h) return;
    this.index += 1;
    this.track.style.transition = 'transform .7s var(--ease)';
    this.track.style.transform = `translateY(${-this.index * h}px)`;
    if (this.index >= this.items.length) {
      const done = () => {
        this.track.style.transition = 'none';
        this.index = 0;
        this.track.style.transform = 'translateY(0)';
      };
      this.track.addEventListener('transitionend', done, { once: true });
    }
  }
}
