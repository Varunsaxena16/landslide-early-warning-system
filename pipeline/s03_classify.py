"""Step 3 - reclassify the ensemble into 5 risk categories, then build the hotspot layer.

The GIS side delivered LSM_ensemble.tif as a CONTINUOUS susceptibility index
(0.0004 … 0.888), not the 1-5 map described in the meeting. The categories the
dashboard shows are therefore derived here, and every break value is written to
data/processed/classification.json so the choice is auditable rather than buried
in code.

Default method is natural breaks (Jenks), the convention in the susceptibility
literature: it puts class boundaries where the histogram is already sparse
instead of imposing an arbitrary grid on a strongly right-skewed distribution.
`quantile` and `equal_interval` are computed alongside it, and step 5 scores all
three against the GSI landslide inventory so the default can be defended.

Category 5 ("Very High") is the hotspot layer asked for in the meeting
(16:16:59). Hotspots are built as 8-connected components so that each polygon
is one contiguous patch. A patch is published only when something is actually
there: WorldPop population or an Open Buildings footprint. Risk on that patch
is the susceptibility index times that exposure — area and road proximity
describe the patch, they do not create risk on an empty slope.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from scipy import ndimage
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.common import (OUT_DIR, VEC_DIR, banner, cog_path, encode, load_registry, log,
                             resolve_grid, write_cog, write_json)
from pipeline.exposure import (FORMULA, combine_exposure, has_exposure, load_exposure_rasters,
                               publish_scores)
from pipeline.risk_lib import keep_risk_hotspot, publish_aliases

EIGHT = np.ones((3, 3), dtype=bool)   # 8-connectivity structuring element


# --------------------------------------------------------------------------- #
# break-finding
# --------------------------------------------------------------------------- #
def natural_breaks(values: np.ndarray, k: int, bins: int = 2048, iters: int = 200) -> list:
    """Jenks-equivalent breaks via histogram-weighted 1-D k-means.

    Exact Fisher-Jenks is O(n^2 k) and unusable on 7.7 M pixels. Binning to a fine
    histogram and running weighted Lloyd's iterations to convergence reproduces the
    same breaks to well within one bin width, in under a second.
    """
    lo, hi = float(values.min()), float(values.max())
    counts, edges = np.histogram(values, bins=bins, range=(lo, hi))
    centres = (edges[:-1] + edges[1:]) / 2
    keep = counts > 0
    centres, counts = centres[keep], counts[keep].astype("float64")

    cum = np.cumsum(counts) / counts.sum()               # deterministic quantile init
    seeds = np.array([centres[min(np.searchsorted(cum, (i + 0.5) / k), len(centres) - 1)]
                      for i in range(k)], dtype="float64")

    for _ in range(iters):
        bounds = (seeds[:-1] + seeds[1:]) / 2
        assign = np.searchsorted(bounds, centres)
        new = seeds.copy()
        for i in range(k):
            m = assign == i
            w = counts[m].sum()
            if w > 0:
                new[i] = float((centres[m] * counts[m]).sum() / w)
        new = np.sort(new)
        if np.allclose(new, seeds, rtol=0, atol=(hi - lo) / bins / 10):
            seeds = new
            break
        seeds = new
    return [round(float(b), 6) for b in (seeds[:-1] + seeds[1:]) / 2]


def quantile_breaks(values, k):
    return [round(float(np.percentile(values, 100 * i / k)), 6) for i in range(1, k)]


def equal_interval_breaks(values, k):
    lo, hi = float(values.min()), float(values.max())
    return [round(lo + (hi - lo) * i / k, 6) for i in range(1, k)]


BREAK_METHODS = {"natural_breaks": natural_breaks, "quantile": quantile_breaks,
                 "equal_interval": equal_interval_breaks}


def apply_breaks(arr: np.ndarray, breaks: list) -> np.ndarray:
    """Continuous index -> class 1..len(breaks)+1; 0 marks no-data."""
    out = np.zeros(arr.shape, dtype="uint8")
    ok = np.isfinite(arr)
    out[ok] = (np.digitize(arr[ok], breaks, right=False) + 1).astype("uint8")
    return out


def read_real(layer_id: str, reg: dict) -> np.ndarray:
    """Read a harmonised COG back as real values, NaN for no-data."""
    s = reg["layers_by_id"][layer_id]["store"]
    with rasterio.open(cog_path(layer_id)) as ds:
        stored = ds.read(1)
    return np.where(stored == s["nodata"], np.nan,
                    stored.astype("float64") * s["scale"] + s["offset"])


# --------------------------------------------------------------------------- #
def main() -> dict:
    banner("STEP 3  ·  RECLASSIFY THE ENSEMBLE + BUILD THE HOTSPOT LAYER")
    reg = load_registry()
    grid = resolve_grid(reg)
    cfg = reg["classification"]
    k = int(cfg["n_classes"])
    cls_cfg = reg["layers_by_id"]["lsm_class"]
    labels = {int(v): d["label"] for v, d in cls_cfg["classes"].items()}
    px_km2 = grid.res ** 2 / 1e6
    px_ha = grid.res ** 2 / 1e4

    lsm = read_real("lsm", reg)
    vals = lsm[np.isfinite(lsm)]
    log("input", f"{vals.size:,} valid pixels, index {vals.min():.4f} … {vals.max():.4f}, "
                 f"median {np.median(vals):.4f}")

    all_breaks = {name: fn(vals, k) for name, fn in BREAK_METHODS.items()}
    if cfg["method"] == "manual":
        all_breaks["manual"] = list(cfg["manual_breaks"])
    chosen = cfg["method"]
    breaks = all_breaks[chosen]
    for name, b in all_breaks.items():
        log("breaks", f"{name:16s} {['%.4f' % x for x in b]}"
                      f"{'   <- in use' if name == chosen else ''}")

    classes = apply_breaks(lsm, breaks)
    valid_px = int((classes > 0).sum())

    dist = []
    for c in range(1, k + 1):
        n = int((classes == c).sum())
        dist.append({
            "class": c, "label": labels.get(c, str(c)), "pixels": n,
            "area_km2": round(n * px_km2, 2),
            "share_pct": round(100 * n / max(valid_px, 1), 2),
            "index_range": [round(float(breaks[c - 2]) if c > 1 else float(vals.min()), 4),
                            round(float(breaks[c - 1]) if c <= len(breaks) else float(vals.max()), 4)],
        })

    print(f"\n  {'class':>5s}  {'label':11s} {'index range':>19s} {'area km²':>11s} {'share':>7s}")
    print("  " + "-" * 58)
    for d in dist:
        print(f"  {d['class']:>5d}  {d['label']:11s} {d['index_range'][0]:>8.4f} – "
              f"{d['index_range'][1]:<8.4f} {d['area_km2']:>11,.1f} {d['share_pct']:>6.1f}%")
    print()

    write_cog(cog_path("lsm_class"),
              encode(np.where(classes == 0, np.nan, classes), cls_cfg["store"]),
              grid, cls_cfg["store"])
    log("cog", f"lsm_class      {cog_path('lsm_class').stat().st_size / 1e6:.2f} MB")

    # ----------------------------------------------------------------- hotspots
    hot = classes == k
    min_px = int(cfg["hotspot_min_pixels"])
    # sieve works on both phases: it drops patches smaller than min_px AND fills
    # holes smaller than min_px, which is what keeps polygon rings from shattering.
    sieved = features.sieve(hot.astype("uint8"), size=min_px, connectivity=8).astype(bool)
    log("sieve", f"class {k}: {int(hot.sum(dtype='int64')):,} px -> "
                 f"{int(sieved.sum(dtype='int64')):,} px  "
                 f"(specks removed: {int((hot & ~sieved).sum(dtype='int64')):,}; "
                 f"holes filled: {int((~hot & sieved).sum(dtype='int64')):,})")

    lab, n_lab = ndimage.label(sieved, structure=EIGHT)
    idx = np.arange(1, n_lab + 1)
    log("label", f"{n_lab:,} 8-connected hotspot patches")

    # ---- zonal attributes: what is actually exposed inside each patch --------
    slope = read_real("slope", reg)
    ntl = read_real("ntl", reg)
    dist_road = read_real("dist_roads", reg)
    worldpop, bld_count_px, bld_area_px = load_exposure_rasters()
    with rasterio.open(cog_path("lulc")) as ds:
        lulc = ds.read(1)
    builtup = (lulc == 1)
    cropland = (lulc == 4)

    def zon(fn, arr, fill=np.nan):
        out = np.asarray(fn(np.nan_to_num(arr, nan=fill), lab, idx), dtype="float64")
        return out

    area_px = np.asarray(ndimage.sum(np.ones_like(lab, dtype="float64"), lab, idx))
    mean_lsm = zon(ndimage.mean, lsm, 0.0)
    max_lsm = zon(ndimage.maximum, lsm, 0.0)
    mean_slope = zon(ndimage.mean, slope, 0.0)
    max_slope = zon(ndimage.maximum, slope, 0.0)
    ntl_sum = zon(ndimage.sum, ntl, 0.0)
    min_road = zon(ndimage.minimum, dist_road, 1e6)
    built_px = np.asarray(ndimage.sum(builtup.astype("float64"), lab, idx))
    crop_px = np.asarray(ndimage.sum(cropland.astype("float64"), lab, idx))
    population = zon(ndimage.sum, worldpop, 0.0)
    building_count = zon(ndimage.sum, bld_count_px, 0.0)
    building_area = zon(ndimage.sum, bld_area_px, 0.0)

    # Exposure is people + building footprint only. Area and roads stay on the
    # feature as description; they must not keep an empty slope on the list.
    exp_cfg = reg.get("exposure") or {}
    exposure = combine_exposure(
        population, building_area,
        pop_weight=float(exp_cfg.get("population_weight", 0.5)),
        building_weight=float(exp_cfg.get("building_weight", 0.5)),
    )

    import pyproj
    to_wgs = pyproj.Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True).transform
    simplify_m = float(cfg["hotspot_simplify_m"])

    recs = {}
    for geom, val in features.shapes(lab, mask=sieved, transform=grid.transform, connectivity=8):
        i = int(val)
        if i == 0:
            continue
        g = shape(geom).simplify(simplify_m, preserve_topology=True)
        if g.is_empty:
            continue
        recs[i] = g

    feats = []
    dropped = 0
    for i, g in recs.items():
        j = i - 1
        pop_j = round(float(population[j]), 1)
        bld_n = int(round(float(building_count[j])))
        # Drop on the published precision, so a patch that would display as
        # "0 people, 0 buildings" never survives as a hotspot.
        if not has_exposure(pop_j, bld_n) or not keep_risk_hotspot(pop_j, bld_n):
            dropped += 1
            continue
        rp = g.representative_point()               # guaranteed inside, unlike a centroid
        lon, lat = to_wgs(rp.x, rp.y)
        mean_index = round(float(mean_lsm[j]), 4)
        exposure_score, risk_score = publish_scores(mean_index, float(exposure[j]))
        aliases = publish_aliases(pop_j, bld_n)
        feats.append({
            "label": i,
            "geom": _round_geom(shp_transform(to_wgs, g), 5),
            "props": {
                "area_ha": round(float(area_px[j] * px_ha), 2),
                "area_km2": round(float(area_px[j] * px_km2), 4),
                "mean_index": mean_index,
                "max_index": round(float(max_lsm[j]), 4),
                "mean_slope_deg": round(float(mean_slope[j]), 1),
                "max_slope_deg": round(float(max_slope[j]), 1),
                "min_dist_road_m": int(min(min_road[j], 99999)),
                "builtup_ha": round(float(built_px[j] * px_ha), 2),
                "cropland_ha": round(float(crop_px[j] * px_ha), 2),
                "ntl_sum": round(float(ntl_sum[j]), 3),
                "population": pop_j,
                "building_count": bld_n,
                "building_area_m2": round(float(building_area[j]), 1),
                "Population_Exposed": aliases["Population_Exposed"],
                "Buildings_At_Risk": aliases["Buildings_At_Risk"],
                "exposure_score": exposure_score,
                "risk_score": risk_score,
                "lon": round(float(lon), 5), "lat": round(float(lat), 5),
            },
        })

    feats.sort(key=lambda f: -f["props"]["area_ha"])
    for rank, f in enumerate(feats, 1):
        f["props"]["rank_area"] = rank
    for rank, f in enumerate(sorted(feats, key=lambda f: -f["props"]["exposure_score"]), 1):
        f["props"]["rank_exposure"] = rank
    for rank, f in enumerate(sorted(feats, key=lambda f: -f["props"]["risk_score"]), 1):
        f["props"]["rank_risk"] = rank
    _reattach_past_slides(feats)
    log("expose", f"dropped {dropped:,} patches with no people and no buildings; "
                  f"{len(feats):,} remain")

    total_ha = sum(f["props"]["area_ha"] for f in feats)
    areas = [f["props"]["area_ha"] for f in feats]
    log("shapes", f"{len(feats):,} polygons, {total_ha / 100:,.1f} km² total, "
                  f"largest {feats[0]['props']['area_km2']:,.1f} km², "
                  f"median {np.median(areas):.2f} ha" if feats else
                  f"{len(feats):,} polygons")

    poly_fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": f["props"]["rank_area"], "properties": f["props"],
         "geometry": mapping(f["geom"])} for f in feats]}
    point_fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": f["props"]["rank_area"], "properties": f["props"],
         "geometry": {"type": "Point",
                      "coordinates": [f["props"]["lon"], f["props"]["lat"]]}} for f in feats]}

    write_json(VEC_DIR / "hotspots.geojson", poly_fc)
    write_json(VEC_DIR / "hotspot_points.geojson", point_fc)
    log("write", f"hotspots.geojson {(VEC_DIR / 'hotspots.geojson').stat().st_size / 1e6:.1f} MB · "
                 f"hotspot_points.geojson "
                 f"{(VEC_DIR / 'hotspot_points.geojson').stat().st_size / 1e6:.2f} MB")

    result = {
        "method": chosen, "n_classes": k, "breaks": breaks, "breaks_all_methods": all_breaks,
        "index_min": round(float(vals.min()), 6), "index_max": round(float(vals.max()), 6),
        "classified_px": valid_px, "distribution": dist,
        "hotspots": {
            "class": k, "min_pixels": min_px, "min_area_ha": round(min_px * px_ha, 2),
            "simplify_m": simplify_m, "count": len(feats),
            "labelled_patches": len(recs),
            "dropped_zero_exposure": dropped,
            "total_area_km2": round(total_ha / 100, 2),
            "largest_area_km2": round(feats[0]["props"]["area_km2"], 3) if feats else 0,
            "median_area_ha": round(float(np.median(areas)), 2) if feats else 0,
            "exposure_formula": FORMULA,
        },
    }
    write_json(OUT_DIR / "classification.json", result)
    return result


def _reattach_past_slides(feats: list) -> None:
    """Keep GSI points pointing at the hotspots that survived the exposure filter.

    Step 4 rebuilds this join from the raw KML when that file is present. Running
    step 3 on its own — the usual path once the COGs exist — still has to move
    hotspot_rank, or a point keeps the rank of a patch that was just dropped.
    """
    slides = {f["props"]["rank_area"]: 0 for f in feats}
    inv_path = VEC_DIR / "inventory.geojson"
    if inv_path.exists() and feats:
        inv = json.load(open(inv_path))
        geoms = [f["geom"] for f in feats]
        ranks = [f["props"]["rank_area"] for f in feats]
        tree = STRtree(geoms)
        for feature in inv["features"]:
            pt = shape(feature["geometry"])
            hit = None
            for j in tree.query(pt):
                if geoms[int(j)].covers(pt):
                    hit = ranks[int(j)]
                    break
            feature["properties"]["hotspot_rank"] = hit
            if hit is not None:
                slides[hit] += 1
        write_json(inv_path, inv)
        log("join", f"{sum(slides.values()):,} inventory points sit in a published hotspot")
    for f in feats:
        f["props"]["past_slides"] = slides[f["props"]["rank_area"]]


def _round_geom(geom, nd: int):
    """Trim coordinate precision - 5 dp is ~1 m, and halves the GeoJSON size."""
    return shp_transform(lambda x, y, z=None: (np.round(x, nd), np.round(y, nd)), geom)


if __name__ == "__main__":
    main()
