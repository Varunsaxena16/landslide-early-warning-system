# Sikkim Landslide Platform

**Panel 1 — landslide susceptibility mapping, hotspot identification and exposure screening for Sikkim, India.**

Smart India Hackathon · GIS / Remote Sensing track · problem statement **26001**, *Early Warning and Landslide Risk Monitoring System in NER* (MDoNER).

An interactive GIS dashboard over the team's existing five-model susceptibility ensemble: every conditioning factor as a toggleable layer, click-to-inspect at any point, Very-High hotspots ranked by what they expose, and the model scored against the Geological Survey of India's landslide inventory.

**Live: https://sikkim-landslide.onrender.com/** — the free instance sleeps when idle, so the first request may take up to a minute to wake.

---

## What this is, and what it is not

This matters more than any feature, and the team agreed it in the 27 August review (16:11:55, 16:12:02):

> *"Kabhi bhi main early warning system to bata hi nahi paaya hoon — main real-time bata paaya hoon."*
> *"Hamesha isko maine 'Rapid' bola hai — Rapid Satellite-Based Detection System."*

| Panel | Question it answers | Status |
|---|---|---|
| **1 · Susceptibility** | Where are slopes predisposed to fail? | **Complete — this project** |
| 2 · Rapid detection | Has a slope just failed, and where? | Model trained and validated (~80%, 1 June 2025 event); not yet wired into the dashboard |
| 3 · Early warning | Is a slope about to fail? | **Not built.** Needs the InSAR deformation track, which is blocked — land-cover change over built-up areas reads as ground subsidence |

Panel 1 is a **static planning map**. It carries no live rainfall, no soil moisture and no deformation signal, so it cannot say a slope is failing today. Present it as susceptibility and rapid detection; do not let "rapid detection" be read as "early warning".

---

## Headline results

| | |
|---|---|
| Study area | **7,108 km²** (Sikkim) |
| Very High ("hotspot") class | **729 km² · 10.5%** of the state |
| High + Very High | 21.0% of the area holds **99.2%** of mapped landslides |
| Validation lift | **4.72×** landslide density versus area alone |
| Success-rate AUC | **0.948** |
| Top 10% of area | captures **81.7%** of mapped landslides |
| Hotspot patches | **485** published (918 labelled, **433** with nobody and no buildings dropped) |
| People in High + Very High | **480,000 of 634,000** (76%) — WorldPop, placed on Open Buildings |
| Buildings in High + Very High | **80%** of detected footprints |
| Built-up exposed | **9.7 km² of 12.7 km²** sits in High or Very High |
| Road network exposed | **74%** of the mapped network |

Scored against **691 Geological Survey of India** inventory points. This is a *success-rate* curve, not a prediction-rate curve — the inventory was almost certainly in the training data, so it measures fit, not out-of-sample skill. See [misc/docs/VALIDATION.md](misc/docs/VALIDATION.md).

---

## Quick start

```bash
make install                       # venv + dependencies
cp -r /path/to/Risk_hotspots .     # the raw delivery (not in git — see misc/docs/DATA.md)
make data                          # ~60 s: 881 MB raw → 62 MB of COGs + vectors
make serve                         # http://localhost:8000
```

Already have `data/processed/`? Then `make install && make serve` is enough — the raw rasters are only needed to rebuild.

```bash
make test                 # pipeline consistency + API contract
docker compose up --build # container on :8080
```

## Deploying

One container serves the API and the dashboard, so a deployment is one service and one URL.
Full instructions in **[misc/docs/DEPLOY.md](misc/docs/DEPLOY.md)**.

Currently deployed at **https://sikkim-landslide.onrender.com/** (dashboard),
[`/api/docs`](https://sikkim-landslide.onrender.com/api/docs) for the API, and
[`/healthz`](https://sikkim-landslide.onrender.com/healthz) for the health check.

| Target | Command |
|---|---|
| Render (fastest public URL) | Deploy using [`render.yaml`](render.yaml) |
| Google Cloud Run | `make deploy PROJECT=<gcp-project-id>` |
| Anywhere else | plain `Dockerfile`; reads `$PORT`, health check on `/healthz` |

The application can be checked locally with `make test`, and the container can be smoke-tested
with `make docker-run`.

---

## What the dashboard does

- **Layer control** — all 11 harmonised layers, grouped, each with an opacity slider, a generated legend and a note on what it is and why it is in the model.
- **Click-to-inspect** — click anywhere and read every conditioning factor at that pixel plus the model's verdict, exactly as asked for in the meeting (16:35:01).
- **Hotspots** — Very-High patches that contain WorldPop population or an Open Buildings footprint. Empty slopes are left off. Each patch carries people, building area, and how many past landslides it contains, and is ranked by risk = susceptibility × exposure.
- **Past landslides** — the full GSI inventory, filterable by district, material, movement type, activity and the model's class at that site.
- **Search** — villages, towns and named landslides; jumps there and inspects it.
- **Statistics** — area breakdown, the validation curve, exposure figures, and cross-tabs showing *which terrain* the model calls dangerous.

Everything the UI draws — layers, groups, legends, colours, labels — comes from [`config/layers.yml`](config/layers.yml). Adding a factor raster is a config entry and `make data`; no JavaScript changes.

## The interface

The design language follows Statskog's [Statskogmillionen](https://www.statskog.no/statskogmillionen):
a deep forest vignette, luminous points, Cormorant Garamond display over Open Sans, skewed
parallelogram controls and hairline rules.

- **Two base maps, Light and Terrain**, on a light theme by default — the presentation the project
  review asked for. Neither style paints an opaque background, so the CSS vignette shows through the
  canvas: if the tile CDN is slow or demo wifi is gone, the risk surface still draws over a coloured
  ground rather than a blank rectangle.
- **Luminous hotspots.** Each published Very-High patch is drawn as three stacked GPU circle
  layers — bloom, halo, core — sized off its **risk score** (susceptibility × exposure), so
  brightness encodes how much is at stake. A slow sine breathes the bloom.
- **The category rail** on the left steps through the five risk classes the way Statskog steps
  through award years, with the same receding-perspective fade. It filters the raster server-side
  (`/api/tiles/lsm_class/...?classes=5`) and the points client-side. <kbd>↑</kbd>/<kbd>↓</kbd> work.
- **Map / List.** The List view is a horizontally scrolling wall of hotspot cards; each card's glow
  is sized by area on a log scale, since the top of the list is uniformly ~100 on exposure.
- **Everything else from the previous build is retained** — all four overlays, all 11 layers
  with opacity sliders and notes, click-to-inspect, the inventory filters, search and the five
  statistics tabs. The layer controls moved into a drawer that opens by default on desktop.

Fonts and MapLibre are vendored (~940 KB total); the only remote calls are the optional raster
base maps.

---

## Architecture

```
raw GeoTIFFs                pipeline/                    data/processed/
(3 resolutions,   ──▶  harmonise → classify →   ──▶   COGs (62 MB)
 2 CRSs, 881 MB)        vectorise → validate           GeoJSON + stats.json
                                                              │
                                                              ▼
                                              backend/  FastAPI + rio-tiler
                                              ├── /api/tiles/{layer}/{z}/{x}/{y}.png
                                              ├── /api/point?lon=&lat=
                                              ├── /api/hotspots, /api/inventory
                                              └── /api/stats
                                                              │
                                                              ▼
                                              frontend/  MapLibre GL (vendored)
```

**Why tiles are rendered on demand** rather than pre-generated: rio-tiler reads the 512 px block it needs at the right overview level, so the repo stays small, every zoom is sharp, and re-running the pipeline is live immediately with no tile cache to invalidate.

**Why one container**: the API and the dashboard are served by the same process, so a deployment is one image and one Cloud Run service.

See [misc/docs/ARCHITECTURE.md](misc/docs/ARCHITECTURE.md), and
**[misc/docs/TECHNICAL_REPORT.md](misc/docs/TECHNICAL_REPORT.md)** for the full reference — every layer's
value range and pixel encoding, the zonal statistics and cross-tabs, how each design decision was
made and why, the bugs worth recording, and a costed plan for the early-warning gap.

---

## Project layout

| Path | What lives there |
|---|---|
| `config/layers.yml` | **The registry.** Single source of truth for the pipeline, the API and the UI |
| `pipeline/` | `s01` inventory → `s02` harmonise → `s03` classify → `s04` GSI points → `s05` statistics |
| `backend/` | FastAPI app: tiles, point queries, features, statistics, static hosting |
| `frontend/` | MapLibre dashboard — no build step, vendored library and glyphs |
| `data/processed/` | COGs, GeoJSON, `stats.json` — what makes this project run without rebuilding data |
| `misc/` | Optional project notes, documentation, and tests; not needed to run the dashboard |
| `reference/` | Sikkim settlement list used by search (approximate — see misc/docs/DATA.md) |
| `tools/` | `build_borders.py` — regenerates the Natural Earth boundary vectors |
| `Dockerfile`, `render.yaml`, `cloudbuild.yaml` | Deployment — see [misc/docs/DEPLOY.md](misc/docs/DEPLOY.md) |

---

## Known limitations

These are stated plainly because a susceptibility map that hides them is worse than no map.

1. **The ensemble does not cover ~139 km²** of southern and eastern Sikkim that is inside the study area. Those pixels return "no model value".
2. **Random Forest over-fits distance-to-road**, painting Very High directly onto road pixels (meeting 16:18:13). The equal-weight ensemble damps this; it does not remove it. Some of the 74% road exposure figure is this artefact.
3. **The GSI inventory is road- and settlement-biased.** Landslides get mapped where people are. The model has seen few examples from the unpopulated high north, so low risk there is weakly evidenced.
4. **Three land-cover labels and all seven lithology unit names were not supplied** with the rasters. Labels marked `?` in the UI were recovered from each class's measured signature — see [misc/docs/DATA.md](misc/docs/DATA.md) for the evidence, and confirm them against the source.
5. **The road vector was not supplied**, only a distance raster. Absolute road-length figures are estimates; the shares are sound.
6. **No village boundaries.** People and buildings now come from WorldPop 2020 and Google Open Buildings. That is a headcount and a footprint, not a list of villages. Census village points would still close the "which village" question.
7. **The five categories were derived here**, not delivered — the ensemble arrived as a continuous index. Breaks and the alternatives are in [misc/docs/VALIDATION.md](misc/docs/VALIDATION.md).

---

## What the GIS side still needs to send

Tracked from the meeting's action items:

- [ ] The remaining conditioning factors — 17 were modelled, **9 arrived**. Missing: aspect, curvature, TWI, elevation, and others.
- [ ] Lithology unit names and the LULC class legend.
- [ ] The road / drainage **vectors**, not just the distance rasters.
- [ ] The variable → data source → purpose table (already written, per 16:33:56).
- [ ] The `.h5` model files, for panel 2.
- [ ] Confirmation of why the ensemble's extent is clipped short of the state boundary.

---

## Licence and attribution

Landslide inventory © Geological Survey of India (Bhukosh). Basemaps © Esri / OpenTopoMap / OpenStreetMap contributors. Rainfall climatology from CHIRPS. Built for the Smart India Hackathon internal round, deadline 3 September.
