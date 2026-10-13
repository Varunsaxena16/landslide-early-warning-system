"""Step 1 - inventory the raw delivery.

Reports what actually arrived from the GIS side: CRS, extent, resolution,
no-data handling and valid-data footprint of every raster, plus the vector
inventory. Writes data/processed/inventory.json and prints a readable report.

Run this first whenever new files land in the shared drive - it is the cheapest
way to catch a raster that was exported in the wrong CRS or with a broken
no-data value before it silently poisons the stack.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.common import OUT_DIR, RAW_DIR, banner, load_registry, log, raw_path, write_json


def probe(layer: dict) -> dict:
    path = raw_path(layer)
    if not path.exists():
        return {"id": layer["id"], "file": layer["file"], "status": "MISSING"}

    with rasterio.open(path) as ds:
        step = max(1, max(ds.width, ds.height) // 1200)
        arr = ds.read(
            1,
            out_shape=(1, max(ds.height // step, 1), max(ds.width // step, 1)),
            resampling=Resampling.nearest,
        ).astype("float64")

        nodata = layer.get("nodata_in") if layer.get("nodata_in") is not None else ds.nodata
        valid = np.isfinite(arr)
        if nodata is not None:
            valid &= ~np.isclose(arr, float(nodata))
            if abs(float(nodata)) > 1e29:
                valid &= arr > -1e29
        if layer.get("zero_is_nodata"):
            valid &= arr != 0

        vals = arr[valid]
        px_area = ds.res[0] * ds.res[1] * (step ** 2)
        out = {
            "id": layer["id"],
            "file": layer["file"],
            "status": "ok",
            "size_mb": round(path.stat().st_size / 1e6, 1),
            "crs": str(ds.crs),
            "shape": [ds.width, ds.height],
            "resolution_m": [round(ds.res[0], 4), round(ds.res[1], 4)],
            "dtype": ds.dtypes[0],
            "declared_nodata": None if ds.nodata is None else float(ds.nodata),
            "nodata_used": None if nodata is None else float(nodata),
            "bounds_wgs84": [round(v, 5) for v in transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)],
            "valid_fraction": round(float(valid.mean()), 4),
            "valid_area_km2_est": round(float(valid.sum() * px_area / 1e6), 1),
        }
        if vals.size:
            out["stats"] = {
                "min": round(float(vals.min()), 4),
                "max": round(float(vals.max()), 4),
                "mean": round(float(vals.mean()), 4),
            }
            uniq = np.unique(vals)
            if uniq.size <= 32:
                out["distinct_values"] = [float(v) for v in uniq]
        return out


def main() -> dict:
    banner("STEP 1  ·  RAW DATA INVENTORY")
    reg = load_registry()
    log("paths", f"RAW_DIR = {RAW_DIR}")

    rasters = [probe(l) for l in reg["layers"] if not l.get("derived")]

    vectors = []
    for vec in reg["vectors"]:
        if vec.get("derived"):
            continue
        path = RAW_DIR / vec["file"]
        entry = {"id": vec["id"], "file": vec["file"]}
        if not path.exists():
            entry["status"] = "MISSING"
        else:
            from pipeline.s04_inventory_points import parse_kml

            feats = parse_kml(path)
            entry.update(
                status="ok",
                size_mb=round(path.stat().st_size / 1e6, 1),
                feature_count=len(feats),
                attributes=sorted({k for f in feats for k in f["properties"]}),
            )
        vectors.append(entry)

    print(f"\n  {'layer':16s} {'crs':11s} {'res':>7s} {'shape':>13s} {'dtype':>8s} "
          f"{'valid km²':>10s}  range")
    print("  " + "-" * 92)
    for r in rasters:
        if r["status"] != "ok":
            print(f"  {r['id']:16s} !! MISSING: {r['file']}")
            continue
        rng = ""
        if "stats" in r:
            rng = f"{r['stats']['min']:g} … {r['stats']['max']:g}"
        print(f"  {r['id']:16s} {r['crs'].replace('EPSG:',''):11s} "
              f"{r['resolution_m'][0]:>7.4g} {str(r['shape']):>13s} {r['dtype']:>8s} "
              f"{r['valid_area_km2_est']:>10.1f}  {rng}")
    for v in vectors:
        if v["status"] == "ok":
            print(f"  {v['id']:16s} {'vector':11s} {'-':>7s} {str(v['feature_count']) + ' pts':>13s} "
                  f"{'-':>8s} {'-':>10s}  {len(v['attributes'])} attributes")

    crs_set = {r["crs"] for r in rasters if r["status"] == "ok"}
    if len(crs_set) > 1:
        log("note", f"mixed CRS in the delivery ({', '.join(sorted(crs_set))}) - "
                    "step 2 reprojects everything onto the analysis grid")

    report = {"raw_dir": str(RAW_DIR), "rasters": rasters, "vectors": vectors}
    write_json(OUT_DIR / "inventory.json", report)
    log("write", str(OUT_DIR / "inventory.json"))
    return report


if __name__ == "__main__":
    main()
