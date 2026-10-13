"""Step 5 - the numbers the dashboard's statistics panel reads.

Four blocks:

  1. AREA        - how much of Sikkim sits in each risk category.

  2. VALIDATION  - the honest check. The ensemble is scored against the 691
     mapped GSI landslides: landslide density per class, and the success-rate
     curve with its AUC. All three classification schemes are scored so the
     default (natural breaks) can be defended rather than asserted.

     Read as a SUCCESS-rate curve, not a prediction-rate curve: the inventory
     was almost certainly part of model training, so this measures fit, not
     out-of-sample skill. Section "caveats" in the output says so.

  3. EXPOSURE    - what is actually at stake in the High and Very High classes:
     WorldPop population, Open Buildings footprint, built-up land cover,
     and an estimate of exposed road length.

  4. CROSS-TABS  - risk class against LULC, lithology, soil and slope bands.
     This is what makes the map explainable: it shows WHICH terrain the model
     is calling dangerous.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.common import (OUT_DIR, VEC_DIR, banner, cog_path, load_registry, log,
                             resolve_grid, write_json)
from pipeline.exposure import load_exposure_rasters
from pipeline.s03_classify import BREAK_METHODS, apply_breaks, read_real

SLOPE_BINS = [0, 15, 25, 35, 45, 90]


def success_rate_curve(index: np.ndarray, point_index: np.ndarray, n=200) -> dict:
    """Share of mapped landslides captured as a function of the share of area ranked most susceptible.

    The standard LSM validation curve. AUC 0.5 = no skill, 1.0 = perfect.
    """
    pts = point_index[np.isfinite(point_index)]
    if pts.size == 0:
        return {}
    srt = np.sort(index)[::-1]                       # descending susceptibility
    fracs = np.linspace(0.005, 1.0, n)
    xs, ys = [], []
    for f in fracs:
        cut = srt[min(int(f * srt.size), srt.size - 1)]
        xs.append(float(f))
        ys.append(float((pts >= cut).mean()))
    auc = float(np.trapezoid(ys, xs)) if hasattr(np, "trapezoid") else float(np.trapz(ys, xs))
    return {"area_fraction": [round(x, 4) for x in xs],
            "landslides_captured": [round(y, 4) for y in ys],
            "auc": round(auc, 4),
            "capture_at_10pct_area": round(float(np.interp(0.10, xs, ys)), 4),
            "capture_at_20pct_area": round(float(np.interp(0.20, xs, ys)), 4),
            "capture_at_30pct_area": round(float(np.interp(0.30, xs, ys)), 4)}


def class_validation(classes: np.ndarray, point_classes: np.ndarray, labels: dict, k: int) -> list:
    total_px = int((classes > 0).sum(dtype="int64"))
    pts = point_classes[np.isfinite(point_classes)]
    rows = []
    for c in range(1, k + 1):
        px = int((classes == c).sum(dtype="int64"))
        n = int((pts == c).sum())
        area_share = px / max(total_px, 1)
        slide_share = n / max(pts.size, 1)
        rows.append({
            "class": c, "label": labels.get(c, str(c)),
            "area_share": round(area_share, 4), "landslides": n,
            "landslide_share": round(slide_share, 4),
            # >1 means landslides concentrate here more than area alone would predict
            "density_ratio": round(slide_share / area_share, 2) if area_share > 0 else None,
        })
    return rows


def _masked_sum(arr: np.ndarray, mask: np.ndarray) -> float:
    return float(np.nansum(np.where(mask, arr, np.nan)))


def _headcount(arr, study, high, vhigh) -> dict:
    total = _masked_sum(arr, study)
    hi = _masked_sum(arr, high)
    vh = _masked_sum(arr, vhigh)
    return {
        "source": "WorldPop 2020 UN-adjusted 1 km, allocated onto Open Buildings centroids",
        "total": round(total, 1),
        "in_high_or_very_high": round(hi, 1),
        "in_very_high": round(vh, 1),
        "share_in_high_or_very_high": round(hi / total, 4) if total else None,
        "share_in_very_high": round(vh / total, 4) if total else None,
        "caveat": "A modelled headcount (WorldPop 1 km), not a census enumeration. "
                  "Each 1 km cell's people are placed onto the Open Buildings centroids inside it, "
                  "so a slope with no detected roof does not inherit the neighbouring village.",
    }


def _buildings(count, area, study, high, vhigh) -> dict:
    def pack(mask):
        return int(round(_masked_sum(count, mask))), round(_masked_sum(area, mask) / 1e6, 3)

    total_n, total_km2 = pack(study)
    hi_n, hi_km2 = pack(high)
    vh_n, vh_km2 = pack(vhigh)
    return {
        "source": "Google Open Buildings v3 centroids, confidence ≥ 0.65",
        "total_count": total_n,
        "total_km2": total_km2,
        "in_high_or_very_high_count": hi_n,
        "in_high_or_very_high_km2": hi_km2,
        "in_very_high_count": vh_n,
        "in_very_high_km2": vh_km2,
        "share_in_high_or_very_high": round(hi_n / total_n, 4) if total_n else None,
        "caveat": "Footprints, not households. A confidence cut of 0.65 drops the weak tail "
                  "of the detector. Area is the sum of footprint m², which can exceed the "
                  "ground area where detections overlap.",
    }


def main() -> dict:
    banner("STEP 5  ·  STATISTICS, VALIDATION AND EXPOSURE")
    reg = load_registry()
    grid = resolve_grid(reg)
    px_km2 = grid.res ** 2 / 1e6
    k = int(reg["classification"]["n_classes"])
    labels = {int(v): d["label"] for v, d in reg["layers_by_id"]["lsm_class"]["classes"].items()}
    classification = json.load(open(OUT_DIR / "classification.json"))

    lsm = read_real("lsm", reg)
    with rasterio.open(cog_path("lsm_class")) as ds:
        classes = ds.read(1)
    with rasterio.open(cog_path("lulc")) as ds:
        lulc = ds.read(1)
    with rasterio.open(cog_path("lithology")) as ds:
        litho = ds.read(1)
    with rasterio.open(cog_path("soil")) as ds:
        soil = ds.read(1)
    slope = read_real("slope", reg)
    ntl = read_real("ntl", reg)
    dist_road = read_real("dist_roads", reg)
    worldpop, building_count, building_area = load_exposure_rasters()

    study = lulc != 255
    classified = classes > 0
    study_km2 = float(study.sum(dtype="int64")) * px_km2

    inv = json.load(open(VEC_DIR / "inventory.geojson"))
    p_index = np.array([f["properties"]["factors"].get("lsm") or np.nan for f in inv["features"]],
                       dtype="float64")
    p_class = np.array([f["properties"]["factors"].get("lsm_class") or np.nan for f in inv["features"]],
                       dtype="float64")

    # ------------------------------------------------------------------ 1 AREA
    area = {
        "study_area_km2": round(study_km2, 1),
        "classified_km2": round(float(classified.sum(dtype="int64")) * px_km2, 1),
        "unclassified_km2": round(float((study & ~classified).sum(dtype="int64")) * px_km2, 1),
        "by_class": classification["distribution"],
    }
    log("area", f"study area {study_km2:,.0f} km²; "
                f"{area['unclassified_km2']:,.0f} km² inside Sikkim has no ensemble value")

    # ------------------------------------------------------------ 2 VALIDATION
    idx_valid = lsm[np.isfinite(lsm)]
    curves, per_class = {}, {}
    for name in list(BREAK_METHODS) + (["manual"] if reg["classification"]["method"] == "manual" else []):
        br = classification["breaks_all_methods"][name]
        cls_n = apply_breaks(lsm, br)
        pc = np.array([np.nan if not np.isfinite(v) else np.digitize(v, br, right=False) + 1
                       for v in p_index])
        per_class[name] = class_validation(cls_n, pc, labels, k)
    curve = success_rate_curve(idx_valid, p_index)

    chosen = classification["method"]
    n_pts = int(np.isfinite(p_index).sum())
    top2 = sum(r["landslide_share"] for r in per_class[chosen] if r["class"] >= k - 1)
    top2_area = sum(r["area_share"] for r in per_class[chosen] if r["class"] >= k - 1)

    validation = {
        "inventory_source": reg["vectors_by_id"]["inventory"]["source"],
        "inventory_total": len(inv["features"]),
        "inventory_scored": n_pts,
        "inventory_outside_lsm_extent": len(inv["features"]) - n_pts,
        "success_rate_curve": curve,
        "by_class": per_class[chosen],
        "by_class_all_methods": per_class,
        "headline": {
            "high_and_very_high_area_share": round(top2_area, 4),
            "high_and_very_high_landslide_share": round(top2, 4),
            "lift": round(top2 / top2_area, 2) if top2_area else None,
            "auc": curve.get("auc"),
        },
        "caveats": [
            "This is a SUCCESS-rate curve, not a prediction-rate curve: the GSI inventory "
            "was very likely used to train the five models, so it measures goodness of fit, "
            "not out-of-sample skill. A held-out split is needed for a true prediction rate.",
            f"{len(inv['features']) - n_pts} of {len(inv['features'])} inventory points fall "
            "outside the ensemble's extent and are excluded from the scoring.",
            "GSI inventory points are mapped at ~1:50,000; a point may sit tens of metres from "
            "the actual scarp, which blurs the value sampled at 30 m.",
        ],
    }
    print()
    print(f"  {'class':>5s}  {'label':11s} {'area %':>8s} {'slides':>7s} {'slides %':>9s} {'density':>8s}")
    print("  " + "-" * 56)
    for r in validation["by_class"]:
        print(f"  {r['class']:>5d}  {r['label']:11s} {100 * r['area_share']:>7.1f}% "
              f"{r['landslides']:>7d} {100 * r['landslide_share']:>8.1f}% "
              f"{r['density_ratio'] if r['density_ratio'] is not None else 0:>7.2f}×")
    print()
    log("valid", f"High + Very High cover {100 * top2_area:.1f}% of the area and contain "
                 f"{100 * top2:.1f}% of mapped landslides  →  lift {top2 / top2_area:.2f}×")
    log("valid", f"success-rate AUC {curve['auc']:.3f}  "
                 f"(top 10% of area captures {100 * curve['capture_at_10pct_area']:.1f}% of landslides)")
    for name in per_class:
        rows = per_class[name]
        t2 = sum(r["landslide_share"] for r in rows if r["class"] >= k - 1)
        t2a = sum(r["area_share"] for r in rows if r["class"] >= k - 1)
        log("      ", f"{name:16s} lift {t2 / t2a:.2f}×  "
                      f"(top-2 classes: {100 * t2a:.1f}% area, {100 * t2:.1f}% slides)"
                      f"{'   <- in use' if name == chosen else ''}")

    # -------------------------------------------------------------- 3 EXPOSURE
    high = classified & (classes >= k - 1)          # High + Very High
    vhigh = classes == k
    # A "road cell" is a pixel whose distance-to-road is under half a pixel, i.e.
    # the road runs through it. Cell count x 30 m approximates centreline length.
    road_cell = np.isfinite(dist_road) & (dist_road <= grid.res / 2)
    ntl_total = float(np.nansum(np.where(study, ntl, np.nan)))

    def km2(mask):
        return round(float(mask.sum(dtype="int64")) * px_km2, 2)

    exposure = {
        "built_up": {
            "total_km2": km2(lulc == 1),
            "in_high_or_very_high_km2": km2((lulc == 1) & high),
            "in_very_high_km2": km2((lulc == 1) & vhigh),
        },
        "roads": {
            # The SHARE is the trustworthy number here: the rasterisation bias that
            # affects the numerator affects the denominator identically. The absolute
            # kilometres are an order-of-magnitude estimate only - see caveats.
            "share_in_high_or_very_high": round(
                float((road_cell & high).sum(dtype="int64")) / max(float(road_cell.sum(dtype="int64")), 1), 4),
            "share_in_very_high": round(
                float((road_cell & vhigh).sum(dtype="int64")) / max(float(road_cell.sum(dtype="int64")), 1), 4),
            "road_cells": int(road_cell.sum(dtype="int64")),
            "estimated_network_km": round(float(road_cell.sum(dtype="int64")) * grid.res / 1000, 1),
            "in_high_or_very_high_km": round(float((road_cell & high).sum(dtype="int64")) * grid.res / 1000, 1),
            "in_very_high_km": round(float((road_cell & vhigh).sum(dtype="int64")) * grid.res / 1000, 1),
            "very_high_within_250m_of_road_km2": km2(vhigh & (dist_road <= 250)),
            "method": "The road vector itself was not supplied, only a distance-to-road "
                      "raster. Cells at distance 0 are taken as carrying the centreline; "
                      "cell count x 30 m approximates length.",
            "caveats": [
                "The estimated ~2,080 km is close to Sikkim's TOTAL road network, not the "
                "~700-900 km of National and State Highways - so despite the file name "
                "'dis_to_Major_roads', the underlying network looks broader than "
                "'major roads'. Ask the GIS side for the road vector before quoting "
                "any absolute figure.",
                "Cell-count x 30 m under-reads diagonal segments by up to 41%.",
            ],
        },
        "population": _headcount(worldpop, study, high, vhigh),
        "buildings": _buildings(building_count, building_area, study, high, vhigh),
        "population_proxy": {
            "metric": "night-time lights radiance, summed",
            "share_in_high_or_very_high": round(
                float(np.nansum(np.where(high, ntl, np.nan))) / ntl_total, 4) if ntl_total else None,
            "share_in_very_high": round(
                float(np.nansum(np.where(vhigh, ntl, np.nan))) / ntl_total, 4) if ntl_total else None,
            "caveat": "Retained as a cross-check. The headcount used for risk is the WorldPop population block.",
        },
        "past_landslides": {
            "total": len(inv["features"]),
            "in_very_high_hotspots": sum(
                1 for f in inv["features"] if f["properties"].get("hotspot_rank") is not None),
        },
        "hotspots": classification["hotspots"],
    }
    log("expose", f"built-up in High/Very High: "
                  f"{exposure['built_up']['in_high_or_very_high_km2']:.1f} km² of "
                  f"{exposure['built_up']['total_km2']:.1f} km² total")
    log("expose", f"roads: {100 * exposure['roads']['share_in_high_or_very_high']:.0f}% of the mapped "
                  f"network sits in High/Very High "
                  f"(~{exposure['roads']['in_high_or_very_high_km']:,.0f} km of an estimated "
                  f"{exposure['roads']['estimated_network_km']:,.0f} km - see caveats)")
    pop = exposure["population"]
    bld = exposure["buildings"]
    log("expose", f"WorldPop in High/Very High: {pop['in_high_or_very_high']:,.0f} of "
                  f"{pop['total']:,.0f} people "
                  f"({100 * pop['share_in_high_or_very_high']:.1f}%)")
    log("expose", f"Open Buildings in High/Very High: {bld['in_high_or_very_high_count']:,} of "
                  f"{bld['total_count']:,} footprints "
                  f"({bld['in_high_or_very_high_km2']:.2f} km²)")

    # ------------------------------------------------------------ 4 CROSS-TABS
    def crosstab(cat: np.ndarray, cfg_id: str):
        cls_cfg = reg["layers_by_id"][cfg_id]
        cat_labels = {int(v): d["label"] for v, d in cls_cfg["classes"].items()}
        rows = []
        for v in sorted(np.unique(cat)):
            if v == cls_cfg["store"]["nodata"]:
                continue
            # Must be masked to the study area. `soil` treats 0 as a real class
            # (unmapped rock and ice), so without this the class-0 row silently
            # absorbs every pixel outside Sikkim and reports 4,027 km2 instead
            # of the true 621 km2.
            m = (cat == v) & study
            n = int(m.sum(dtype="int64"))
            in_high = int((m & high).sum(dtype="int64"))
            rows.append({"value": int(v), "label": cat_labels.get(int(v), f"class {v}"),
                         "area_km2": round(n * px_km2, 2),
                         "high_or_very_high_km2": round(in_high * px_km2, 2),
                         "share_high_or_very_high": round(in_high / n, 4) if n else 0})
        return sorted(rows, key=lambda r: -r["share_high_or_very_high"])

    slope_rows = []
    for lo, hi in zip(SLOPE_BINS[:-1], SLOPE_BINS[1:]):
        m = study & np.isfinite(slope) & (slope >= lo) & (slope < hi)
        n = int(m.sum(dtype="int64"))
        in_high = int((m & high).sum(dtype="int64"))
        slope_rows.append({"label": f"{lo}–{hi}°", "min": lo, "max": hi,
                           "area_km2": round(n * px_km2, 2),
                           "high_or_very_high_km2": round(in_high * px_km2, 2),
                           "share_high_or_very_high": round(in_high / n, 4) if n else 0})

    cross = {"lulc": crosstab(lulc, "lulc"), "lithology": crosstab(litho, "lithology"),
             "soil": crosstab(soil, "soil"), "slope": slope_rows}
    # Rank the headline only among classes big enough for the share to mean something -
    # a 3 km2 class can hit 80% on noise alone.
    material = [r for r in cross["lulc"] if r["area_km2"] >= 50]
    top = material[0] if material else cross["lulc"][0]
    log("cross", f"most exposed land cover (>= 50 km²): {top['label']} — "
                 f"{100 * top['share_high_or_very_high']:.0f}% of its {top['area_km2']:,.0f} km² "
                 f"is High/Very High")

    stats = {
        "generated_for": reg["project"]["study_area"],
        "grid": grid.to_json(),
        "classification": {"method": classification["method"], "breaks": classification["breaks"]},
        "area": area, "validation": validation, "exposure": exposure, "cross_tabs": cross,
    }
    write_json(OUT_DIR / "stats.json", stats)
    log("write", f"stats.json ({(OUT_DIR / 'stats.json').stat().st_size / 1024:.0f} KB)")
    return stats


if __name__ == "__main__":
    main()
