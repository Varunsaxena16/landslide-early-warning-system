"""Step 2 - harmonise every factor raster onto one analysis grid, as COGs.

The delivery arrives in three resolutions (10 m, 30 m, ~5 km), two CRSs
(UTM 45N and geographic) and four different no-data conventions. Nothing
downstream - point queries, cross-tabs, tiles - can be trusted until they all
sit on the same lattice.

Output: data/processed/cog/<id>.tif
  - EPSG:32645, 30 m, identical shape and transform for every layer
  - integer-encoded (real = stored * scale + offset) with an explicit no-data
  - internally tiled + overviews, so the tile server reads a 512 px block
    instead of the whole raster
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.common import (COG_DIR, OUT_DIR, banner, build_grid, cog_path, encode, extend_to_study,
                             load_registry, log, read_onto_grid, source_layers, write_cog, write_json)


def main() -> dict:
    banner("STEP 2  ·  HARMONISE ONTO THE ANALYSIS GRID")
    reg = load_registry()
    grid = build_grid(reg)
    gj = grid.to_json()
    log("grid", f"{grid.width} × {grid.height} @ {grid.res:g} m  {grid.crs}  "
                f"({grid.width * grid.height / 1e6:.1f} M px)")
    log("grid", f"bbox WGS84 {gj['bounds_wgs84']}")

    # The study-area mask defines "inside Sikkim" for every downstream statistic.
    mask_id = reg["project"]["mask_from"]
    mask_arr = read_onto_grid(reg["layers_by_id"][mask_id], grid)
    study_mask = np.isfinite(mask_arr)
    area_km2 = float(study_mask.sum()) * grid.res ** 2 / 1e6
    log("mask", f"study area from '{mask_id}': {int(study_mask.sum()):,} px = {area_km2:,.0f} km²")

    summary = {"grid": gj, "study_area_km2": round(area_km2, 1), "layers": {}}
    COG_DIR.mkdir(parents=True, exist_ok=True)

    for layer in source_layers(reg):
        lid = layer["id"]
        arr = mask_arr.copy() if lid == mask_id else read_onto_grid(layer, grid)

        # Distance rasters ship as full rectangles with no no-data at all; without
        # this they would paint values across Nepal, Bhutan and West Bengal.
        if layer.get("mask_with_study_area"):
            arr[~study_mask] = np.nan
        # The ensemble stops short of the southern and eastern outline. Continue
        # the nearest value so every factor meets the study area the map draws.
        arr = extend_to_study(arr, study_mask)

        finite = np.isfinite(arr)
        inside = finite & study_mask
        coverage = float(inside.sum()) / float(study_mask.sum())

        out = write_cog(cog_path(lid), encode(arr, layer["store"]), grid, layer["store"])
        entry = {
            "path": str(out.relative_to(OUT_DIR)),
            "size_mb": round(out.stat().st_size / 1e6, 2),
            "coverage_of_study_area": round(coverage, 4),
            "valid_px": int(finite.sum()),
            "store": layer["store"],
        }
        if layer["kind"] == "continuous" and inside.any():
            v = arr[inside]
            entry["stats"] = {
                "min": float(np.min(v)), "max": float(np.max(v)),
                "mean": float(np.mean(v)), "std": float(np.std(v)),
                "p2": float(np.percentile(v, 2)), "p98": float(np.percentile(v, 98)),
            }
        else:
            vals, counts = np.unique(arr[inside].astype("int64"), return_counts=True)
            entry["class_area_km2"] = {
                int(v): round(float(c) * grid.res ** 2 / 1e6, 2) for v, c in zip(vals, counts)
            }
        summary["layers"][lid] = entry

        gap = "" if coverage > 0.995 else f"   ⚠ {(1 - coverage) * 100:.1f}% of the study area has no value"
        log("cog", f"{lid:14s} {entry['size_mb']:6.2f} MB   coverage {coverage * 100:5.1f}%{gap}")

    write_json(OUT_DIR / "grid.json", gj)
    write_json(OUT_DIR / "harmonise.json", summary)
    total = sum(v["size_mb"] for v in summary["layers"].values())
    log("done", f"{len(summary['layers'])} COGs, {total:.1f} MB total "
                f"(raw delivery was {sum(p.stat().st_size for p in _raw_files()) / 1e6:,.0f} MB)")
    return summary


def _raw_files():
    from pipeline.common import RAW_DIR
    return [p for p in RAW_DIR.rglob("*") if p.is_file()]


if __name__ == "__main__":
    main()
