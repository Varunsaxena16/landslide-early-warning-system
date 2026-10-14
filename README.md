# Pahiro.Watch

**Landslide susceptibility, hotspot exposure and rainfall-triggered early warning for Sikkim, India.**

*Pahiro* is Nepali for landslide. Pahiro.Watch is an interactive GIS dashboard built on a five-model susceptibility ensemble. It shows where Sikkim's slopes are predisposed to fail, who lives and travels beneath them, and which of those places are under heavy rain right now.

**Live: https://sikkim-landslide-dashboard-9mf9.onrender.com/**. It runs on a free instance that sleeps when idle, so the first load can take up to a minute.

---

## Two views

| View | Question it answers | What it shows |
|---|---|---|
| **Risk map** | Where are slopes predisposed to fail, and who is exposed? | Susceptibility surface, 5 risk classes, 11 conditioning-factor layers, 485 ranked hotspots, the GSI landslide inventory |
| **Early warning** | Which exposed slopes are getting heavy rain now? | Live rainfall, rain- and soil-moisture alerts on hotspots, highway advisories, nearest safe assembly points, a regional hazard bulletin |

**What it is not.** The risk map is a static planning layer, so it shows where slopes are *prone* to fail, not that one is failing today. The early-warning view adds a time trigger (rainfall and soil moisture) on top of it. It does not measure ground movement, and its alert thresholds are rule-based, not calibrated against past failures.

---

## Headline results

| | |
|---|---|
| Study area | **7,108 km²** (Sikkim) |
| Very High ("hotspot") class | **729 km² · 10.5%** of the state |
| High + Very High | 21.0% of the area holds **99.2%** of mapped landslides |
| Validation lift | **4.72×** landslide density versus area alone |
| Top 10% of area | captures **81.7%** of mapped landslides |
| Success-rate AUC | **0.948** |
| Hotspot patches | **485** published, out of 918 found (433 with no people or buildings dropped) |
| People in High + Very High | **480,000 of 634,000** (76%), from WorldPop placed on Open Buildings |
| Buildings in High + Very High | **80%** of detected footprints |
| Road network exposed | **74%** of the mapped network |

These figures are scored against **691 Geological Survey of India** inventory points. It's a *success-rate* curve, not a prediction-rate curve: the inventory was very likely used in training, so it measures fit, not out-of-sample skill.

---

## What the dashboard does

### Risk map
- **Layer control.** All 11 harmonised layers, grouped, each with an opacity slider, a generated legend, and a note on what it is and why it's in the model.
- **Click-to-inspect.** Click anywhere to read every conditioning factor at that pixel, plus the model's verdict.
- **Hotspots.** Very-High patches that contain population or building footprints, ranked by risk = susceptibility × exposure. Empty slopes are left out.
- **Past landslides.** The full GSI inventory, filterable by district, material, movement type, activity, and the model's class at that site.
- **Search.** Villages, towns and named landslides; it jumps to the place and inspects it.
- **Statistics.** Area breakdown, the validation curve, exposure figures, and cross-tabs showing which terrain the model calls dangerous.

### Early warning
- **Live rainfall.** Observed precipitation and a short nowcast from LibreWXR (NOAA RRQPE), animated over the risk surface.
- **Hotspot alerts.** Hourly rain and topsoil moisture from Open-Meteo, checked at every published hotspot:

  | Level | Trigger |
  |---|---|
  | Watch | soil moisture ≥ 0.40 m³/m³ |
  | Warning | ≥ 10 mm in 3 h or ≥ 40 mm in 24 h |
  | Critical | ≥ 25 mm in 3 h or ≥ 80 mm in 24 h |

- **Highway advisories.** NH10 and the North Sikkim Highway are cut into 1 km sections and scored against risk polygons and past slides.
- **Safe assembly points.** Schools, grounds and halls from OpenStreetMap that sit more than 100 m from both risk polygons and recorded landslides. A click returns the nearest ones.
- **Hazard bulletin.** M4+ earthquakes within 420 km of Gangtok (USGS), GDACS flood and quake alerts, and regional news headlines.
- **Community reports.** Unverified reports of warning signs, such as cracks or tilting trees, pinned to the map.

Everything the UI draws (layers, groups, legends, colours, labels) comes from [`config/layers.yml`](config/layers.yml). Adding a factor raster takes one config entry and `make data`, with no JavaScript changes.

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
                                   ├── /api/point, /api/search
                                   ├── /api/hotspots, /api/inventory, /api/stats
                                   └── /api/alerts, /api/live-rain, /api/bulletin,
                                       /api/corridor, /api/shelters
                                                              │
                                                              ▼
                                   frontend/  MapLibre GL (vendored, no build step)
```

- **Tiles are rendered on demand.** rio-tiler reads only the block it needs at the right overview level, so the repo stays small, every zoom is sharp, and a re-run of the pipeline goes live with no tile cache to clear.
- **One container.** The API and the dashboard are served by the same process, so a deployment is one image and one URL.
- **No API keys.** Every live feed (Open-Meteo, LibreWXR, USGS, GDACS) is public. A failed refresh keeps the last good copy.

**Stack:** Python, FastAPI, rasterio / GDAL, rio-tiler, Shapely, pyproj, SciPy, MapLibre GL JS, Docker, Render.

---

## Run it locally

```bash
make install     # venv + dependencies
make serve       # http://localhost:8000
```

`data/processed/` is committed, so nothing needs rebuilding. To run the container instead:

```bash
docker compose up --build   # http://localhost:8080
```

API docs are served at `/api/docs`.

## Deploy

| Target | How |
|---|---|
| Render | New → Blueprint, using [`render.yaml`](render.yaml) |
| Google Cloud Run | `make deploy PROJECT=<gcp-project-id>` |
| Anywhere else | Plain `Dockerfile`; reads `$PORT`, health check on `/healthz` |

---

## Project layout

| Path | What lives there |
|---|---|
| `config/layers.yml` | **The registry.** Single source of truth for the pipeline, the API and the UI |
| `pipeline/` | `s01` inventory → `s02` harmonise → `s03` classify → `s04` GSI points → `s05` statistics, plus alerts, exposure and highway logic |
| `backend/` | FastAPI app: tiles, point queries, hotspots, alerts, bulletin, static hosting |
| `frontend/` | MapLibre dashboard, with vendored library and fonts |
| `data/processed/` | COGs, GeoJSON and `stats.json`, enough to run without rebuilding |
| `reference/` | Sikkim settlement list used by search (approximate) |
| `tools/` | Fetchers for exposure, highways, shelters, alerts and boundary vectors |
| `Dockerfile`, `render.yaml`, `cloudbuild.yaml` | Deployment |

---

## Known limitations

1. **The ensemble does not cover about 139 km²** of southern and eastern Sikkim inside the study area. Those pixels return "no model value".
2. **Random Forest over-fits distance-to-road**, which paints Very High onto road pixels. The equal-weight ensemble damps this but doesn't remove it, so part of the 74% road-exposure figure is this artefact.
3. **The GSI inventory is road- and settlement-biased.** Landslides get mapped where people are, so low risk in the unpopulated high north is weakly evidenced.
4. **Some class labels are inferred.** Three land-cover labels and all seven lithology unit names were recovered from each class's measured signature and are marked `?` in the UI.
5. **Road lengths are estimates.** Only a distance-to-road raster was available, not the road vector. Shares are sound; absolute lengths are approximate.
6. **Exposure is a headcount, not a village list.** People and buildings come from WorldPop 2020 and Google Open Buildings.
7. **The five risk classes were derived here** from a continuous susceptibility index.
8. **Early-warning thresholds are not calibrated** against past rainfall-triggered failures in Sikkim, and the view doesn't measure ground deformation.

---

## Credits and data

Built as a team project by students at Netaji Subhas University of Technology (NSUT), Delhi.

Landslide inventory © Geological Survey of India (Bhukosh). Population: WorldPop 2020. Buildings: Google Open Buildings. Highways and assembly points © OpenStreetMap contributors. Live rainfall: LibreWXR (CC-BY-4.0). Forecasts: Open-Meteo. Earthquakes: USGS. Alerts: GDACS. Rainfall climatology: CHIRPS. Basemaps © Esri / OpenTopoMap / OpenStreetMap contributors.