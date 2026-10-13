/* The List view — Statskog's "Liste" pane.

   Theirs is a horizontally scrolling wall of project cards, each with an
   illustration, a place eyebrow, a serif title and the amount awarded. Ours
   carries hotspots: the "illustration" is a generated glow whose size and warmth
   track the patch's risk score, and the amount becomes what is actually at
   stake there — people, building footprint, past failures.

   Two rows, scrolling sideways, so the eye reads the ranked list as a landscape
   rather than a table. The table lives in Statistics for anyone who wants it. */
import { api, esc, fmt } from './api.js';

export class ListView {
  constructor(els, onOpen) {
    this.els = els;
    this.onOpen = onOpen;
    this.features = null;
    this.filterClass = null;
  }

  async open(rail) {
    this.els.root.hidden = false;
    if (!this.features) {
      this.els.track.innerHTML = `<div class="note-box">Loading hotspots…</div>`;
      const fc = await api.hotspots({ geometry: 'point', limit: 300, order_by: 'risk_score' });
      this.features = fc.features;
    }
    this._render(rail);
  }

  close() { this.els.root.hidden = true; }

  _render(rail) {
    const rows = this.features;
    this.els.sub.textContent =
      `${fmt.num(rows.length)} Very-High patches · ranked by risk` +
      (rail && rail.cls && rail.cls !== 5 ? ` · rail shows ${rail.label}` : '');

    /* Glow size tracks AREA on a log scale. Risk is what the list is sorted by;
       area is what stops the top cards from looking identical. */
    const areas = rows.map(f => Math.log10(Math.max(f.properties.area_km2, 0.005)));
    const lo = Math.min(...areas), hi = Math.max(...areas);
    const norm = v => (hi - lo < 1e-9 ? 0.5 : (Math.log10(Math.max(v, 0.005)) - lo) / (hi - lo));

    this.els.track.innerHTML = rows.map((f, i) => {
      const p = f.properties;
      const mag = norm(p.area_km2);                       // 0 = smallest patch, 1 = largest
      const heat = Math.max(0, Math.min(1, p.risk_score / 100));
      const size = 58 + mag * 118;
      const core = `rgba(255,255,255,${0.5 + heat * 0.42})`;
      const mid = `rgba(255,${196 - heat * 66},${150 - heat * 96},${0.22 + heat * 0.2})`;
      const road = p.min_dist_road_m >= 99999 ? 'no road mapped'
        : p.min_dist_road_m === 0 ? 'road runs through it'
        : `${fmt.num(p.min_dist_road_m)} m to a road`;
      return `
        <article class="card" data-rank="${p.rank_area}" tabindex="0"
                 style="animation-delay:${Math.min(i, 24) * 28}ms">
          <div class="card-viz" style="background:
              radial-gradient(circle at 50% 34%, rgba(19,88,78,.85), rgba(0,32,27,.96) 72%)"></div>
          <div class="card-glow" style="width:${size}px;height:${size}px;
              background:radial-gradient(circle, ${core}, ${mid} 34%, transparent 70%)"></div>
          <div class="card-body">
            <div class="card-eyebrow">Hotspot #${p.rank_area} · risk rank ${p.rank_risk}</div>
            <h3 class="card-title">${fmt.area(p.area_km2)}</h3>
            <div class="card-metric"><b>${p.risk_score.toFixed(0)}</b> / 100 risk</div>
            <div class="card-foot">
              <span>${p.population ? fmt.num(p.population, 0) + ' people' : 'no people'}</span>
              <span>${p.building_count ? fmt.num(p.building_count) + ' buildings' : 'no buildings'}</span>
              <span>${esc(road)}</span>
              ${p.past_slides ? `<span>${p.past_slides} past slides</span>` : ''}
            </div>
          </div>
        </article>`;
    }).join('');

    this.els.track.querySelectorAll('.card').forEach(c => {
      const go = () => this.onOpen(Number(c.dataset.rank));
      c.addEventListener('click', go);
      c.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
    });
  }
}
