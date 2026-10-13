/* The statistics drawer: five tabs over /api/stats.
   Charts are hand-drawn SVG — no charting library, so nothing to load and
   nothing to keep in sync with the theme. */
import { api, esc, fmt } from './api.js';

const RISK_COLORS = { 1: '#1a9850', 2: '#a6d96a', 3: '#fee08b', 4: '#f46d43', 5: '#a50026' };

export class StatsDrawer {
  constructor(view, els, onHotspot) {
    this.view = view; this.els = els; this.onHotspot = onHotspot;
    this.tab = 'summary'; this.data = null; this.hotspots = null;

    els.root.querySelectorAll('.dt').forEach(b => b.addEventListener('click', () => {
      els.root.querySelectorAll('.dt').forEach(x => x.classList.toggle('is-active', x === b));
      this.tab = b.dataset.tab;
      this._render();
    }));
    els.close.addEventListener('click', () => this.close());
  }

  async open() {
    this.els.root.hidden = false;
    document.body.classList.add('has-drawer');
    this.els.btn.setAttribute('aria-expanded', 'true');
    if (!this.data) {
      this.els.body.innerHTML = `<div class="note-box">Loading statistics…</div>`;
      try { this.data = await api.stats(); }
      catch (e) { this.els.body.innerHTML = `<div class="note-box">${esc(e.message)}</div>`; return; }
    }
    this._render();
  }

  close() {
    this.els.root.hidden = true;
    document.body.classList.remove('has-drawer');
    this.els.btn.setAttribute('aria-expanded', 'false');
  }

  toggle() { this.els.root.hidden ? this.open() : this.close(); }

  _render() {
    const d = this.data;
    if (!d) return;
    const fn = { summary: 'summary', validation: 'validation', exposure: 'exposure',
                 terrain: 'terrain', hotspots: 'hotspotTable' }[this.tab];
    this.els.body.innerHTML = this[`_${fn}`](d);
    if (this.tab === 'hotspots') this._wireHotspotTable();
  }

  /* ------------------------------------------------------------- summary -- */
  _summary(d) {
    const a = d.area, v = d.validation, h = d.exposure.hotspots;
    const vh = a.by_class.find(c => c.class === 5) || {};
    const hi = a.by_class.find(c => c.class === 4) || {};
    return `
      <div class="kpis">
        ${kpi('Study area', fmt.num(a.study_area_km2), 'km²', 'Sikkim, from the LULC footprint')}
        ${kpi('Very High', fmt.num(vh.area_km2), 'km²', `${vh.share_pct}% of the state — the hotspot class`)}
        ${kpi('High + Very High', fmt.pct(v.headline.high_and_very_high_area_share, 1), '',
              `contains ${fmt.pct(v.headline.high_and_very_high_landslide_share, 1)} of mapped landslides`)}
        ${kpi('Validation lift', `${v.headline.lift}×`, '',
              'landslide density vs. area alone — 1× would be no skill')}
        ${kpi('Hotspot patches', fmt.num(h.count), '',
              `≥ ${h.min_area_ha} ha, with people or buildings` +
              (h.dropped_zero_exposure ? ` · ${fmt.num(h.dropped_zero_exposure)} empty slopes left off` : ''))}
        ${kpi('Mapped landslides', fmt.num(v.inventory_total), '', 'GSI national inventory, Sikkim')}
      </div>

      <div class="sec">
        <div class="sec-t">Risk categories <small>natural breaks on the ensemble index</small></div>
        ${classTable(a.by_class, v.by_class)}
      </div>`;
  }

  /* ---------------------------------------------------------- validation -- */
  _validation(d) {
    const v = d.validation, c = v.success_rate_curve;
    return `
      <div class="kpis">
        ${kpi('Success-rate AUC', c.auc?.toFixed(3) ?? '—', '', '0.5 = no skill · 1.0 = perfect')}
        ${kpi('Top 10% of area', fmt.pct(c.capture_at_10pct_area, 1), '', 'of mapped landslides captured')}
        ${kpi('Top 20% of area', fmt.pct(c.capture_at_20pct_area, 1), '', 'of mapped landslides captured')}
        ${kpi('Points scored', `${fmt.num(v.inventory_scored)}`, `/ ${fmt.num(v.inventory_total)}`,
              `${v.inventory_outside_lsm_extent} fall outside the ensemble extent`)}
      </div>

      <div class="two-col sec">
        <div>
          <div class="sec-t">Success-rate curve
            <small>share of landslides captured vs. share of area</small></div>
          ${curveSvg(c)}
        </div>
        <div>
          <div class="sec-t">Landslide density by class
            <small>&gt; 1× means landslides concentrate there</small></div>
          ${densityTable(v.by_class)}
          <div class="sec-t" style="margin-top:15px">Break method comparison
            <small>why natural breaks</small></div>
          ${methodTable(v.by_class_all_methods, d.classification.method)}
        </div>
      </div>

      <div class="caveats"><b>What this number is not.</b>
        <ul>${v.caveats.map(c => `<li>${esc(c)}</li>`).join('')}</ul></div>`;
  }

  /* ------------------------------------------------------------ exposure -- */
  _exposure(d) {
    const e = d.exposure, b = e.built_up, r = e.roads, p = e.population, bld = e.buildings;
    return `
      <div class="kpis">
        ${kpi('People exposed', fmt.num(p.in_high_or_very_high, 0), '',
              `${fmt.pct(p.share_in_high_or_very_high, 1)} of ${fmt.num(p.total, 0)} · WorldPop`)}
        ${kpi('Buildings exposed', fmt.num(bld.in_high_or_very_high_count), '',
              `${fmt.pct(bld.share_in_high_or_very_high, 0)} of footprints · ${fmt.num(bld.in_high_or_very_high_km2, 2)} km²`)}
        ${kpi('Built-up exposed', fmt.num(b.in_high_or_very_high_km2, 1), 'km²',
              `of ${fmt.num(b.total_km2, 1)} km² land-cover — ${fmt.pct(b.in_high_or_very_high_km2 / b.total_km2, 0)}`)}
        ${kpi('Road network exposed', fmt.pct(r.share_in_high_or_very_high, 0), '',
              `≈ ${fmt.num(r.in_high_or_very_high_km)} km of ≈ ${fmt.num(r.estimated_network_km)} km`)}
        ${kpi('Very High near roads', fmt.num(r.very_high_within_250m_of_road_km2, 0), 'km²',
              'within 250 m of the road network')}
        ${kpi('Past slides in hotspots', fmt.num(e.past_landslides.in_very_high_hotspots), '',
              `of ${fmt.num(e.past_landslides.total)} mapped`)}
        ${kpi('Largest hotspot', fmt.num(e.hotspots.largest_area_km2, 0), 'km²',
              `median patch is ${e.hotspots.median_area_ha} ha`)}
      </div>

      <div class="caveats"><b>Where these numbers are soft.</b>
        <ul>
          <li>${esc(p.caveat)}</li>
          <li>${esc(bld.caveat)}</li>
          ${(r.caveats || []).map(c => `<li>${esc(c)}</li>`).join('')}
          <li>Village boundaries are still not in the delivery, so this is a headcount inside
              High and Very High terrain, not a list of villages.</li>
        </ul></div>`;
  }

  /* ------------------------------------------------------------- terrain -- */
  _terrain(d) {
    const c = d.cross_tabs;
    return `
      <div class="sec-t">Which terrain the model calls dangerous
        <small>share of each class that falls in High or Very High</small></div>
      <div class="two-col">
        <div>${crossTable('Slope band', c.slope)}${crossTable('Land cover', c.lulc)}</div>
        <div>${crossTable('Lithology', c.lithology)}${crossTable('Soil', c.soil)}</div>
      </div>
      <div class="caveats"><b>This is the explainability view.</b>
        If a category with almost no area shows a very high share, treat it as noise. Lithology unit
        names and some land-cover labels were not supplied with the rasters — see the "?" markers.</div>`;
  }

  /* ------------------------------------------------------- hotspot table -- */
  _hotspotTable(d) {
    if (!this.hotspots) {
      api.hotspots({ geometry: 'point', limit: 200, order_by: 'risk_score' })
        .then(fc => { this.hotspots = fc.features; if (this.tab === 'hotspots') this._render(); });
      return `<div class="note-box">Loading hotspots…</div>`;
    }
    return `
      <div class="sec-t">Top 200 hotspots by risk
        <small>susceptibility × exposure · click a row to open it on the map</small></div>
      <div class="tbl-wrap"><table class="tbl">
        <thead><tr>
          <th>#</th><th class="num">Risk</th><th class="num">Exposure</th><th class="num">People</th>
          <th class="num">Buildings</th><th class="num">Area</th><th class="num">Past slides</th>
        </tr></thead>
        <tbody>${this.hotspots.map(f => {
          const p = f.properties;
          return `<tr class="clickable" data-rank="${p.rank_area}">
            <td>${p.rank_risk}</td>
            <td class="num">${p.risk_score.toFixed(1)}</td>
            <td class="num">${p.exposure_score.toFixed(1)}</td>
            <td class="num">${p.population ? fmt.num(p.population, 0) : '—'}</td>
            <td class="num">${p.building_count ? fmt.num(p.building_count) : '—'}</td>
            <td class="num">${fmt.area(p.area_km2)}</td>
            <td class="num">${p.past_slides || '—'}</td>
          </tr>`;
        }).join('')}</tbody>
      </table></div>`;
  }

  _wireHotspotTable() {
    this.els.body.querySelectorAll('tr[data-rank]').forEach(tr =>
      tr.addEventListener('click', () => this.onHotspot(Number(tr.dataset.rank))));
  }
}

/* ------------------------------------------------------------- fragments -- */
function kpi(label, value, unit, sub) {
  return `<div class="kpi">
    <div class="kpi-l">${esc(label)}</div>
    <div class="kpi-v">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ''}</div>
    <div class="kpi-s">${esc(sub || '')}</div></div>`;
}

function classTable(byClass, validation) {
  const vmap = Object.fromEntries((validation || []).map(v => [v.class, v]));
  const max = Math.max(...byClass.map(c => c.share_pct));
  return `<table class="tbl">
    <thead><tr><th>Class</th><th>Index range</th><th class="num">Area</th>
      <th class="num">Share</th><th style="width:130px"></th>
      <th class="num">Mapped slides</th></tr></thead>
    <tbody>${byClass.map(c => `<tr>
      <td><span class="cls-pill"><i style="background:${RISK_COLORS[c.class]}"></i>${esc(c.label)}</span></td>
      <td class="num">${c.index_range[0].toFixed(3)} – ${c.index_range[1].toFixed(3)}</td>
      <td class="num">${fmt.num(c.area_km2)} km²</td>
      <td class="num">${c.share_pct}%</td>
      <td><div class="rowbar"><i style="width:${(100 * c.share_pct / max).toFixed(1)}%;
        background:${RISK_COLORS[c.class]}"></i></div></td>
      <td class="num">${vmap[c.class] ? fmt.num(vmap[c.class].landslides) : '—'}</td>
    </tr>`).join('')}</tbody></table>`;
}

function densityTable(rows) {
  const max = Math.max(...rows.map(r => r.density_ratio || 0));
  return `<table class="tbl">
    <thead><tr><th>Class</th><th class="num">Area</th><th class="num">Slides</th>
      <th class="num">Density</th><th style="width:96px"></th></tr></thead>
    <tbody>${rows.map(r => `<tr>
      <td><span class="cls-pill"><i style="background:${RISK_COLORS[r.class]}"></i>${esc(r.label)}</span></td>
      <td class="num">${fmt.pct(r.area_share, 1)}</td>
      <td class="num">${r.landslides}</td>
      <td class="num">${r.density_ratio?.toFixed(2) ?? '—'}×</td>
      <td><div class="rowbar"><i style="width:${(100 * (r.density_ratio || 0) / max).toFixed(1)}%;
        background:${RISK_COLORS[r.class]}"></i></div></td>
    </tr>`).join('')}</tbody></table>`;
}

function methodTable(all, chosen) {
  return `<table class="tbl">
    <thead><tr><th>Method</th><th class="num">Top-2 area</th><th class="num">Top-2 slides</th>
      <th class="num">Lift</th></tr></thead>
    <tbody>${Object.entries(all).map(([name, rows]) => {
      const t2 = rows.filter(r => r.class >= 4);
      const a = t2.reduce((s, r) => s + r.area_share, 0);
      const l = t2.reduce((s, r) => s + r.landslide_share, 0);
      return `<tr${name === chosen ? ' style="background:rgba(245,158,11,.09)"' : ''}>
        <td>${esc(name.replace('_', ' '))}${name === chosen ? ' <span class="chip">in use</span>' : ''}</td>
        <td class="num">${fmt.pct(a, 1)}</td><td class="num">${fmt.pct(l, 1)}</td>
        <td class="num">${(l / a).toFixed(2)}×</td></tr>`;
    }).join('')}</tbody></table>`;
}

function crossTable(title, rows) {
  const shown = rows.filter(r => r.area_km2 >= 1).slice(0, 9);
  return `<div class="sec" style="margin-top:0;margin-bottom:17px">
    <div class="sec-t" style="font-size:11.5px">${esc(title)}</div>
    <table class="tbl">
      <thead><tr><th>${esc(title)}</th><th class="num">Area</th>
        <th class="num">High+</th><th style="width:92px"></th></tr></thead>
      <tbody>${shown.map(r => `<tr>
        <td>${esc(r.label)}</td>
        <td class="num">${fmt.num(r.area_km2, 0)} km²</td>
        <td class="num">${fmt.pct(r.share_high_or_very_high, 0)}</td>
        <td><div class="rowbar"><i style="width:${(100 * r.share_high_or_very_high).toFixed(1)}%;
          background:#f46d43"></i></div></td>
      </tr>`).join('')}</tbody></table></div>`;
}

function curveSvg(c) {
  if (!c || !c.area_fraction) return '<div class="note-box">No curve available.</div>';
  const W = 430, H = 225, P = { l: 42, r: 12, t: 12, b: 32 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const X = t => P.l + t * iw, Y = v => P.t + (1 - v) * ih;
  const pts = c.area_fraction.map((x, i) => `${X(x).toFixed(1)},${Y(c.landslides_captured[i]).toFixed(1)}`);
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const m10 = c.capture_at_10pct_area;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img"
      aria-label="Success rate curve, AUC ${c.auc}">
    ${ticks.map(t => `<line class="grid" x1="${P.l}" x2="${W - P.r}" y1="${Y(t)}" y2="${Y(t)}"/>`).join('')}
    ${ticks.map(t => `<text class="lbl" x="${P.l - 6}" y="${Y(t) + 3.5}" text-anchor="end">${(t * 100).toFixed(0)}%</text>`).join('')}
    ${ticks.map(t => `<text class="lbl" x="${X(t)}" y="${H - 12}" text-anchor="middle">${(t * 100).toFixed(0)}%</text>`).join('')}
    <line class="diag" x1="${X(0)}" y1="${Y(0)}" x2="${X(1)}" y2="${Y(1)}"/>
    <polygon class="fillArea" points="${X(0)},${Y(0)} ${pts.join(' ')} ${X(1)},${Y(0)}"/>
    <polyline class="curve" points="${pts.join(' ')}"/>
    <circle class="mk" cx="${X(0.1)}" cy="${Y(m10)}" r="3.6"/>
    <text class="mklbl" x="${X(0.1) + 8}" y="${Y(m10) + 4}">10% area → ${(m10 * 100).toFixed(0)}% of slides</text>
    <line class="axis" x1="${P.l}" y1="${P.t}" x2="${P.l}" y2="${H - P.b}"/>
    <line class="axis" x1="${P.l}" y1="${H - P.b}" x2="${W - P.r}" y2="${H - P.b}"/>
    <text class="lbl" x="${P.l + iw / 2}" y="${H - 1}" text-anchor="middle">share of area, ranked most susceptible first</text>
  </svg>`;
}
