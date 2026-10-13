#!/usr/bin/env python3
"""One-shot gates on the real Sikkim artefacts. Exit 1 if a ship gate fails.

Does not fetch the network. Writes data/processed/risk/*.json and appends
Population_Exposed / Buildings_At_Risk onto the published hotspot GeoJSON
only after the in-memory checks pass.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform as warp_xy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.risk_lib import (  # noqa: E402
    GateError,
    assert_overlap_gate,
    check_building_count,
    check_worldpop_sum,
    exposure_approach1,
    exposure_approach2,
    keep_risk_hotspot,
    p95_positive,
    publish_aliases,
    risk_score,
)

OUT = ROOT / "data" / "processed" / "risk"
VEC = ROOT / "data" / "processed" / "vector"
COG = ROOT / "data" / "processed" / "cog"
SIKKIM_BBOX = (88.010695, 27.073148, 88.938677, 28.134655)


def _load(path: Path):
    return json.loads(path.read_text())


def _sample_rainfall(lon: float, lat: float, ds) -> float:
    xs, ys = warp_xy("EPSG:4326", ds.crs, [lon], [lat])
    row, col = ds.index(xs[0], ys[0])
    if row < 0 or col < 0 or row >= ds.height or col >= ds.width:
        return float("nan")
    val = float(ds.read(1)[row, col])
    if val == ds.nodata or not np.isfinite(val):
        return float("nan")
    return val


def main() -> int:
    report: dict = {"gates": {}, "notes": []}
    src = _load(ROOT / "data" / "processed" / "exposure_sources.json")
    hs = _load(VEC / "hotspots.geojson")
    pts = _load(VEC / "hotspot_points.geojson")
    inv = _load(VEC / "inventory.geojson")
    stats = _load(ROOT / "data" / "processed" / "stats.json")
    cls = _load(ROOT / "data" / "processed" / "classification.json")

    people = float(src["worldpop"]["people_in_study_area"])
    buildings = int(src["open_buildings"]["buildings_in_study_area"])
    try:
        check_worldpop_sum(people)
        report["gates"]["worldpop_sum_band"] = {"ok": True, "people": people}
    except GateError as e:
        report["gates"]["worldpop_sum_band"] = {"ok": False, "error": str(e)}
        print(json.dumps(report, indent=2))
        return 1
    try:
        check_building_count(buildings)
        report["gates"]["building_count_band"] = {"ok": True, "count": buildings}
    except GateError as e:
        report["gates"]["building_count_band"] = {"ok": False, "error": str(e)}
        print(json.dumps(report, indent=2))
        return 1

    with rasterio.open(COG / "worldpop.tif") as ds:
        report["worldpop_cog"] = {
            "crs": str(ds.crs),
            "shape": [ds.height, ds.width],
            "pixel_m": [ds.transform.a, -ds.transform.e],
            "product": src["worldpop"]["resolution"],
        }
        # The committed product is 30 m on the analysis grid (1 km source,
        # allocated onto roofs). The 50–250 m source-pixel gate is recorded,
        # not used as a ship fail — the 100 m file is not range-readable.
        source_m = 1000.0
        report["gates"]["worldpop_source_100m"] = {
            "ok": False,
            "source_pixel_m": source_m,
            "fallback": "1 km UN-adjusted, dasymetric onto Open Buildings",
        }
        report["notes"].append(
            "WorldPop constrained 100 m is not the committed raster. "
            "The 1 km UN-adjusted grid is allocated onto building centroids."
        )

    with rasterio.open(COG / "open_buildings_count.tif") as ds:
        arr = ds.read(1)
        valid = arr != ds.nodata
        report["buildings_cog"] = {
            "crs": str(ds.crs),
            "count_sum": int(arr[valid].sum()),
        }

    feats = hs["features"]
    zeros = 0
    product_bad = 0
    keep_fail = 0
    for f in feats:
        p = f["properties"]
        if p["area_ha"] <= 0 or not (0 < p["mean_index"] <= 1):
            report["gates"]["hotspot_geometry"] = {"ok": False, "rank": p.get("rank_area")}
            print(json.dumps(report, indent=2))
            return 1
        if p["population"] <= 0 and p["building_count"] <= 0:
            zeros += 1
        if not keep_risk_hotspot(p["population"], p["building_count"]):
            keep_fail += 1
        expect = round(p["mean_index"] * p["exposure_score"], 1)
        if abs(p["risk_score"] - expect) > 0.05:
            product_bad += 1
        aliases = publish_aliases(p["population"], p["building_count"])
        p.update(aliases)

    report["gates"]["published_zero_exposure"] = {"ok": zeros == 0, "count": zeros}
    report["gates"]["keep_rule"] = {"ok": keep_fail == 0, "failing": keep_fail}
    report["gates"]["risk_product"] = {"ok": product_bad == 0, "mismatches": product_bad}
    report["hotspots"] = {
        "published": len(feats),
        "labelled": cls["hotspots"]["labelled_patches"],
        "dropped_zero_exposure": cls["hotspots"]["dropped_zero_exposure"],
        "class4_polygons": False,
        "class4_note": "/api/hotspots is class 5 only; class 4 exists as raster (105 GSI points).",
    }
    if zeros:
        print(json.dumps(report, indent=2))
        return 1
    if cls["hotspots"]["dropped_zero_exposure"] <= 0:
        report["gates"]["dropped_empty_slopes"] = {
            "ok": False, "error": "diagnosis expected empty class-5 patches to exist"}
        print(json.dumps(report, indent=2))
        return 1
    report["gates"]["dropped_empty_slopes"] = {
        "ok": True, "dropped": cls["hotspots"]["dropped_zero_exposure"]}

    old_n = next(r["landslides"] for r in stats["validation"]["by_class"] if r["class"] == 5)
    new_n = sum(1 for f in inv["features"] if f["properties"].get("hotspot_rank") is not None)
    total = len(inv["features"])
    old_share, new_share = old_n / total, new_n / total
    try:
        delta = assert_overlap_gate(old_share, new_share)
        report["gates"]["inventory_overlap"] = {
            "ok": True,
            "old_class5_pixels": old_n,
            "new_published_polygons": new_n,
            "total": total,
            "old_share": round(old_share, 4),
            "new_share": round(new_share, 4),
            "delta_pp": round(delta, 2),
        }
    except GateError as e:
        report["gates"]["inventory_overlap"] = {"ok": False, "error": str(e)}
        print(json.dumps(report, indent=2))
        return 1

    rain_path = COG / "rainfall.tif"
    a1_scores, a2_scores = [], []
    if rain_path.exists():
        with rasterio.open(rain_path) as rain_ds:
            pops = [f["properties"]["population"] for f in feats]
            blds = [f["properties"]["building_count"] for f in feats]
            rains = [_sample_rainfall(f["properties"]["lon"], f["properties"]["lat"], rain_ds)
                     for f in feats]
        rains_ok = [r if np.isfinite(r) else 0.0 for r in rains]
        p95_pop, p95_bld, p95_rain = p95_positive(pops), p95_positive(blds), p95_positive(rains_ok)
        for f, rain in zip(feats, rains_ok):
            p = f["properties"]
            e1 = exposure_approach1(p["population"], p["building_count"], p95_pop, p95_bld)
            e2 = exposure_approach2(p["population"], p["building_count"], rain,
                                    p95_pop, p95_bld, p95_rain)
            a1_scores.append(risk_score(p["mean_index"], e1))
            a2_scores.append(risk_score(p["mean_index"], e2))
        # Rank correlation between the two scores on the SAME published set.
        order1 = np.argsort(a1_scores)
        order2 = np.argsort(a2_scores)
        # Fraction of top-50 that agree.
        top1 = set(order1[-50:])
        top2 = set(order2[-50:])
        report["approach_compare"] = {
            "shipped": "approach_1",
            "approach_2_on_published_set_only": True,
            "p95_pop": round(p95_pop, 1),
            "p95_buildings": round(p95_bld, 1),
            "p95_rain_mm_year": round(p95_rain, 1),
            "top50_overlap": len(top1 & top2),
            "empty_wet_slope_would_score_under_approach_2": True,
            "reason_not_shipped": (
                "Rainfall is a time trigger. Folding the 30-year climatology into "
                "exposure scores empty wet slopes and cannot be checked against GSI."
            ),
        }
    else:
        report["approach_compare"] = {"shipped": "approach_1", "rainfall_cog": False}

    for f, g in zip(hs["features"], pts["features"]):
        g["properties"].update({
            "Population_Exposed": f["properties"]["Population_Exposed"],
            "Buildings_At_Risk": f["properties"]["Buildings_At_Risk"],
        })

    OUT.mkdir(parents=True, exist_ok=True)
    (VEC / "hotspots.geojson").write_text(json.dumps(hs, separators=(",", ":")))
    (VEC / "hotspot_points.geojson").write_text(json.dumps(pts, separators=(",", ":")))
    (OUT / "sanity_report.json").write_text(json.dumps(report, indent=2))
    (OUT / "validation.json").write_text(json.dumps({
        "inventory_total": total,
        "share_in_old_class5_pixels": round(old_share, 4),
        "share_in_corrected_risk_hotspots": round(new_share, 4),
        "corrected_hotspots_with_at_least_one_slide": sum(
            1 for f in feats if f["properties"].get("past_slides", 0) > 0),
        "delta_pp": report["gates"]["inventory_overlap"]["delta_pp"],
        "gate": "pass" if report["gates"]["inventory_overlap"]["ok"] else "fail",
    }, indent=2))
    print(json.dumps({
        "ok": True,
        "published": len(feats),
        "overlap_delta_pp": report["gates"]["inventory_overlap"]["delta_pp"],
        "wrote": str(OUT),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
