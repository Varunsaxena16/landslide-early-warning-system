#!/usr/bin/env python3
"""Cross-check the processed Sikkim layers and write a credibility report.

Reads data/processed only. Does not call Open-Meteo. Exit 1 when the
population total leaves 400k–900k, a raster misses the analysis grid, or a
GeoJSON coordinate sits outside the Sikkim box.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.alerts import forecast_url  # noqa: E402
from pipeline.credibility import (  # noqa: E402
    PROPOSALS,
    forecast_anomalies,
    grid_issues,
    legend_cap,
    point_outside,
    population_check,
)
from pipeline.exposure import WORLDPOP_STORE  # noqa: E402
from pipeline.risk_lib import GateError  # noqa: E402
from pipeline.surfaces import RAIN_STORE  # noqa: E402

OUT = ROOT / "data" / "processed" / "risk" / "credibility_audit.json"
GRID_PATH = ROOT / "data" / "processed" / "grid.json"
COG = ROOT / "data" / "processed" / "cog"
PROCESSED = ROOT / "data" / "processed"


def _grid() -> dict:
    return json.loads(GRID_PATH.read_text())


def _profile(ds) -> dict:
    t = ds.transform
    return {
        "crs": ds.crs.to_string() if ds.crs else None,
        "width": ds.width,
        "height": ds.height,
        "transform": [t.a, t.b, t.c, t.d, t.e, t.f],
    }


def _decode(arr: np.ndarray, store: dict) -> np.ndarray:
    nodata = store["nodata"]
    return np.where(arr == nodata, np.nan, arr.astype("float64") * store["scale"] + store["offset"])


def _coords(node, out: list) -> None:
    if node is None:
        return
    if isinstance(node, dict):
        if node.get("type") == "GeometryCollection":
            for geom in node.get("geometries") or []:
                _coords(geom, out)
            return
        _coords(node.get("coordinates"), out)
        return
    if isinstance(node, (list, tuple)):
        if node and isinstance(node[0], (int, float)):
            out.append((float(node[0]), float(node[1])))
            return
        for item in node:
            _coords(item, out)


def _audit_rasters(grid: dict) -> list[dict]:
    rows = []
    for path in sorted(COG.glob("*.tif")):
        with rasterio.open(path) as ds:
            issues = grid_issues(_profile(ds), grid)
        rows.append({"file": path.name, "ok": not issues, "issues": issues})
    return rows


def _audit_vectors(bbox: tuple) -> list[dict]:
    rows = []
    for path in sorted(PROCESSED.rglob("*.geojson")):
        doc = json.loads(path.read_text())
        coords: list = []
        if doc.get("type") == "FeatureCollection":
            for feat in doc.get("features") or []:
                _coords((feat or {}).get("geometry"), coords)
        else:
            _coords(doc, coords)
        outside = sum(1 for lon, lat in coords if point_outside(lon, lat, bbox))
        rows.append({
            "file": str(path.relative_to(PROCESSED)),
            "vertices": len(coords),
            "outside": outside,
            "ok": outside == 0 and len(coords) > 0,
        })
    return rows


def _population(grid_note: dict) -> dict:
    path = COG / "population.tif"
    with rasterio.open(path) as ds:
        people = _decode(ds.read(1), WORLDPOP_STORE)
    occupied = people[np.isfinite(people) & (people > 0)]
    total = float(occupied.sum()) if occupied.size else 0.0
    try:
        report = population_check(total)
    except GateError as e:
        report = {"ok": False, "people": round(total, 1), "error": str(e)}
    if occupied.size:
        p99 = float(np.percentile(occupied, 99))
        report["percentiles"] = {
            "p50": round(float(np.percentile(occupied, 50)), 2),
            "p90": round(float(np.percentile(occupied, 90)), 2),
            "p99": round(p99, 2),
            "max": round(float(occupied.max()), 1),
        }
        report["legend_cap"] = legend_cap(p99)
        report["occupied_pixels"] = int(occupied.size)
    report["grid"] = grid_note
    return report


def _forecast() -> dict:
    path = COG / "rain_24h.tif"
    with rasterio.open(path) as ds:
        mm = _decode(ds.read(1), RAIN_STORE)
    valid = np.isfinite(mm)
    zeros = int(np.count_nonzero(valid & (mm == 0)))
    days = int(parse_qs(urlparse(forecast_url([27.5], [88.5])).query)["forecast_days"][0])
    requested = days * 24
    horizon = forecast_anomalies([], hours=requested)
    cache_path = OUT.parent / "forecast_cache.json"
    cache = {"present": False}
    if cache_path.exists():
        doc = json.loads(cache_path.read_text())
        pts = list((doc.get("points") or {}).values())
        cache = {
            "present": True,
            "fetched_at": doc.get("fetched_at"),
            "points": len(pts),
            "zero_24h_points": sum(1 for p in pts if p.get("precip_24h_mm") == 0),
            "null_hours_logged": sum(int(p.get("precip_null_hours") or 0) for p in pts),
            "points_with_null_counter": sum(1 for p in pts if "precip_null_hours" in p),
        }
    return {
        "valid_pixels": int(valid.sum()),
        "nodata_pixels": int((~valid).sum()),
        "zero_pixels": zeros,
        "min_mm": None if not valid.any() else round(float(mm[valid].min()), 3),
        "max_mm": None if not valid.any() else round(float(mm[valid].max()), 3),
        "zeros_are_nodata": False,
        "note": (
            "A stored 0 on the rain raster is a dry cell inside the study mask. "
            "Nodata is the mask, not a missing API value. A null forecast hour "
            "is counted, then added as 0 so the 24 h total stays defined. "
            "The on-disk cache keeps summaries, so a 0 mm point cannot be "
            "recovered into a dropout after the fact."
        ),
        "requested_hours": requested,
        "covers_72h": requested >= 72,
        "horizon_note": (
            f"Open-Meteo is asked for {requested} h, so 72 h is not covered."
            if requested < 72 else horizon["horizon_note"]
        ),
        "cache": cache,
    }


def main() -> int:
    grid = _grid()
    bbox = tuple(grid["bounds_wgs84"])
    rasters = _audit_rasters(grid)
    vectors = _audit_vectors(bbox)
    pop_row = next(r for r in rasters if r["file"] == "population.tif")
    population = _population(pop_row)
    rain = _forecast()
    failed = (
        not population.get("ok")
        or any(not row["ok"] for row in rasters)
        or any(not row["ok"] for row in vectors)
    )
    report = {
        "ok": not failed,
        "crs": {"stored": "EPSG:32645", "display": "EPSG:4326", "bounds_wgs84": list(bbox)},
        "rasters": rasters,
        "vectors": vectors,
        "population": population,
        "forecast": rain,
        "proposals": list(PROPOSALS),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}  ok={report['ok']}")
    if population.get("people") is not None:
        print(
            f"population {population['people']}  "
            f"census 2011 {population.get('census_2011')}  "
            f"legend cap {population.get('legend_cap')}"
        )
    print(rain["horizon_note"])
    if failed:
        bad_r = [r["file"] for r in rasters if not r["ok"]]
        bad_v = [r["file"] for r in vectors if not r["ok"]]
        if bad_r:
            print("raster issues:", ", ".join(bad_r))
        if bad_v:
            print("vector issues:", ", ".join(bad_v))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
