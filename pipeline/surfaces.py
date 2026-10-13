"""Live surfaces for the early-warning map.

Three COGs, all on the 30 m analysis grid, clipped to the study-area outline:

  rain_24h        Open-Meteo hourly precipitation, summed over the next 24 h
  soil_moisture   Open-Meteo soil_moisture_0_to_7cm at the current hour
  population      WorldPop people already in cog/worldpop.tif, zeros hidden

The rain and soil grids are sampled at 0.1° (about 9 km), the spacing of the
ECMWF IFS forecast Open-Meteo serves, then bilinearly placed on the analysis
grid. They are the same variables the hotspot alerts use, drawn as a surface
instead of a point. They do not change the static risk score.

CHIRPS is a pentad product (preliminary file two days after the pentad ends).
NASA IMERG Early is half-hourly at 0.1° but the granules sit behind an
Earthdata login. Neither is fetched. NASA SMAP is not fetched; the soil layer
is the ECMWF 0–7 cm volumetric water that Open-Meteo already returns.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

from pipeline.alerts import (
    SIKKIM_BBOX, batches, forecast_url, request_json, summarise_hourly, _as_list,
)
from pipeline.common import (
    COG_DIR, OUT_DIR, cog_path, encode, extend_to_study, load_grid, write_cog, write_json,
)
from pipeline.exposure import WORLDPOP_COG, WORLDPOP_STORE
from pipeline.risk_lib import GateError, check_forecast_point

RISK_DIR = OUT_DIR / "risk"
SOURCES_PATH = RISK_DIR / "live_surfaces.json"
STEP_DEG = 0.1
FRESH_HOURS = 6

RAIN_STORE = {"dtype": "uint16", "scale": 0.1, "offset": 0, "nodata": 65535}
SOIL_STORE = {"dtype": "uint16", "scale": 0.0001, "offset": 0, "nodata": 65535}

# Documented once, written beside the rasters. The dashboard layer notes are
# the short form of this.
RESEARCH = {
    "rainfall": {
        "layer_id": "rain_24h",
        "api": "https://api.open-meteo.com/v1/forecast",
        "variable": "hourly precipitation, preceding-hour sum, totalled for the next 24 hours",
        "units": "mm",
        "model": (
            "Same Open-Meteo forecast call as the hotspot alerts. "
            "Open-Meteo documents hourly precipitation and soil_moisture_0_to_7cm "
            "on the ECMWF IFS HRES forecast: 9 km, updated every 6 hours "
            "(https://open-meteo.com/en/docs/ecmwf-api)."
        ),
        "grid": "Sampled at 0.1°, bilinear onto the 30 m analysis grid, clipped to the study-area outline.",
        "not_used": [
            {
                "name": "CHIRPS v3",
                "why": (
                    "Pentad rainfall. The preliminary file is released two days after "
                    "a pentad ends, and the final file the following month. It is a "
                    "monitoring climatology, not a forecast. The 30-year mean already "
                    "in cog/rainfall.tif stays the static conditioning factor."
                ),
            },
            {
                "name": "NASA GPM IMERG Early",
                "why": (
                    "0.1° half-hourly satellite rain, about 4 hours late, but the "
                    "granules are on NASA GES DISC and need an Earthdata login. Not fetched."
                ),
            },
        ],
    },
    "soil_moisture": {
        "layer_id": "soil_moisture",
        "api": "https://api.open-meteo.com/v1/forecast",
        "variable": "soil_moisture_0_to_7cm at the current hour",
        "units": "m³/m³",
        "model": (
            "ECMWF IFS volumetric soil water in the top 7 cm, via Open-Meteo. "
            "The alert gate already accepts 0 to 0.7. The watch line used on "
            "occupied slopes is 0.40."
        ),
        "not_used": [
            {
                "name": "NASA SMAP",
                "why": (
                    "SMAP L4 is a 9 km soil-moisture analysis, but it is behind "
                    "Earthdata and is not fetched. This layer is the forecast "
                    "the alerts already read, not a SMAP map."
                ),
            },
        ],
    },
    "population": {
        "layer_id": "population",
        "file": "cog/worldpop.tif",
        "units": "people per 30 m pixel",
        "model": (
            "WorldPop 2020, UN-adjusted, unconstrained 1 km India grid, "
            "dasymetrically placed onto Google Open Buildings centroids. "
            "Pixels with nobody counted are left transparent so the drape "
            "shows where people are, not a flat wash. Census village tables "
            "are not in this extract."
        ),
        "citation": (
            "WorldPop and CIESIN, Columbia University (2018). Global High "
            "Resolution Population Denominators Project. Bill and Melinda Gates "
            "Foundation (OPP1134076). https://www.worldpop.org"
        ),
    },
}


def axes(step: float = STEP_DEG, bbox: tuple = SIKKIM_BBOX) -> tuple[np.ndarray, np.ndarray]:
    """Cell centres covering Sikkim, on a halo that stays inside the forecast gate.

    The alert gate rejects a point more than 0.1° outside the state box, so the
    halo stops at 0.09°.
    """
    west, south, east, north = bbox
    margin = 0.09

    def line(lo: float, hi: float) -> np.ndarray:
        start = np.round(np.floor(lo / step) * step + step / 2, 5)
        if start < lo - 1e-9:
            start = np.round(start + step, 5)
        vals = []
        x = float(start)
        while x <= hi + 1e-9:
            vals.append(round(x, 5))
            x = round(x + step, 5)
        return np.asarray(vals, dtype="float64")

    lons = line(west - margin, east + margin)
    lats = line(south - margin, north + margin)[::-1]
    if lons.size < 2 or lats.size < 2:
        raise GateError("forecast grid does not cover Sikkim")
    if lons[0] > west or lons[-1] < east or lats[0] < north or lats[-1] > south:
        raise GateError("forecast grid does not cover Sikkim")
    return lons, lats


def fill_grids(lons: np.ndarray, lats: np.ndarray, summaries: dict) -> tuple[np.ndarray, np.ndarray]:
    """summaries[(j, i)] -> rain and soil arrays, north-up, NaN where missing."""
    rain = np.full((lats.size, lons.size), np.nan, dtype="float64")
    soil = np.full_like(rain, np.nan)
    for (j, i), row in summaries.items():
        rain[j, i] = row["precip_24h_mm"]
        if row.get("soil_moisture_0_to_7cm") is not None:
            soil[j, i] = row["soil_moisture_0_to_7cm"]
    return rain, soil


def sample_open_meteo(lons: np.ndarray, lats: np.ndarray, get=None, now=None) -> dict:
    """(j, i) -> summarise_hourly result. Halo points must still pass the Sikkim gate."""
    now = now or datetime.now(timezone.utc).astimezone()
    points = [
        {"j": j, "i": i, "lon": float(lon), "lat": float(lat)}
        for j, lat in enumerate(lats)
        for i, lon in enumerate(lons)
    ]
    out = {}
    for batch in batches(points):
        url = forecast_url((p["lat"] for p in batch), (p["lon"] for p in batch))
        payload = _as_list(request_json(url, get=get))
        if len(payload) != len(batch):
            raise GateError(
                f"Open-Meteo returned {len(payload)} series for {len(batch)} cells"
            )
        for spec, block in zip(batch, payload):
            hourly = block.get("hourly") or {}
            summary = summarise_hourly(hourly, now)
            hourly_max = max(
                (float(x) for x in (hourly.get("precipitation") or [0]) if x is not None),
                default=0.0,
            )
            check_forecast_point(
                spec["lat"], spec["lon"], hourly_max,
                summary["soil_moisture_0_to_7cm"], SIKKIM_BBOX)
            out[(spec["j"], spec["i"])] = summary
    if len(out) != len(points):
        raise GateError("a forecast cell was dropped")
    return out


def _src_transform(lons: np.ndarray, lats: np.ndarray, step: float):
    # Raster rows run north to south. lons/lats are cell centres.
    return from_origin(float(lons[0] - step / 2), float(lats[0] + step / 2), step, step)


def place_on_grid(src: np.ndarray, lons: np.ndarray, lats: np.ndarray,
                  step: float, grid, study: np.ndarray) -> np.ndarray:
    dest = np.full((grid.height, grid.width), -9999.0, dtype="float32")
    reproject(
        source=np.where(np.isfinite(src), src, -9999).astype("float32"),
        destination=dest,
        src_transform=_src_transform(lons, lats, step),
        src_crs="EPSG:4326",
        src_nodata=-9999,
        dst_transform=grid.transform,
        dst_crs=grid.crs,
        dst_nodata=-9999,
        resampling=Resampling.bilinear,
    )
    out = dest.astype("float64")
    out[np.isclose(out, -9999)] = np.nan
    out[~study] = np.nan
    # Bilinear stops half a cell short of the outline. Continue the last
    # forecast value so rain and soil meet the same edge as the other layers.
    return extend_to_study(out, study)


def study_mask(grid) -> np.ndarray:
    """Land-cover footprint: the outline the map draws, which reaches further
    south and east than the ensemble raster."""
    path = cog_path("lulc")
    if not path.exists():
        raise GateError(f"missing {path}")
    with rasterio.open(path) as ds:
        if (ds.width, ds.height) != (grid.width, grid.height):
            raise GateError("land-cover raster is not on the analysis grid")
        nodata = ds.nodata if ds.nodata is not None else 255
        return ds.read(1) != nodata


def people_surface(stored: np.ndarray, store: dict = WORLDPOP_STORE) -> np.ndarray:
    """Decode WorldPop. Zero and nodata become NaN so empty ground stays clear."""
    nodata = store["nodata"]
    people = stored.astype("float64") * store["scale"] + store["offset"]
    people[stored == nodata] = np.nan
    people[~np.isfinite(people) | (people <= 0)] = np.nan
    return people


def _stats(arr: np.ndarray) -> dict:
    v = arr[np.isfinite(arr)]
    if v.size == 0:
        return {"min": None, "max": None, "mean": None, "pixels": 0}
    return {
        "min": round(float(v.min()), 4),
        "max": round(float(v.max()), 4),
        "mean": round(float(v.mean()), 4),
        "pixels": int(v.size),
    }


def sources_are_fresh(path: Path = SOURCES_PATH, hours: float = FRESH_HOURS,
                      now: datetime | None = None) -> bool:
    if not path.exists():
        return False
    if not cog_path("rain_24h").exists() or not cog_path("soil_moisture").exists():
        return False
    try:
        fetched = datetime.fromisoformat(json.loads(path.read_text())["fetched_at"])
    except (OSError, KeyError, ValueError):
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - fetched <= timedelta(hours=hours)


def write_population() -> dict:
    if not WORLDPOP_COG.exists():
        raise GateError(f"missing {WORLDPOP_COG}")
    grid = load_grid()
    with rasterio.open(WORLDPOP_COG) as ds:
        stored = ds.read(1)
    people = people_surface(stored)
    write_cog(cog_path("population"), encode(people, WORLDPOP_STORE), grid, WORLDPOP_STORE)
    return _stats(people)


def write_forecast_surfaces(get=None, now=None, step: float = STEP_DEG) -> dict:
    grid = load_grid()
    study = study_mask(grid)
    lons, lats = axes(step)
    summaries = sample_open_meteo(lons, lats, get=get, now=now)
    rain_src, soil_src = fill_grids(lons, lats, summaries)
    rain = place_on_grid(rain_src, lons, lats, step, grid, study)
    soil = place_on_grid(soil_src, lons, lats, step, grid, study)
    write_cog(cog_path("rain_24h"), encode(rain, RAIN_STORE), grid, RAIN_STORE)
    write_cog(cog_path("soil_moisture"), encode(soil, SOIL_STORE), grid, SOIL_STORE)
    return {
        "cells_lon": int(lons.size),
        "cells_lat": int(lats.size),
        "step_deg": step,
        "rain_24h": _stats(rain),
        "soil_moisture": _stats(soil),
    }


def refresh_surfaces(get=None, now=None, force: bool = False) -> dict:
    """Write population from the local WorldPop COG, and rain/soil when stale."""
    RISK_DIR.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    population = write_population()
    if not force and sources_are_fresh(now=now):
        doc = json.loads(SOURCES_PATH.read_text())
        doc["population_stats"] = population
        return doc
    forecast = write_forecast_surfaces(get=get, now=now)
    doc = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "timezone": "Asia/Kolkata",
        "population_stats": population,
        "forecast": forecast,
        **RESEARCH,
    }
    write_json(SOURCES_PATH, doc)
    return doc
