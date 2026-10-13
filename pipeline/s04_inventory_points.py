"""Step 4 - the GSI landslide inventory: parse, enrich, and join to the hotspots.

Past_LS_points.kml is the Geological Survey of India national landslide
inventory (Bhukosh), Sikkim extract - 691 mapped historical failures carrying
trigger, material, movement type, dimensions and damage attributes.

It does two jobs in this project:
  1. an overlay - "where has this actually happened before"
  2. the independent check on the model - what share of real, mapped landslides
     fall inside the High / Very High categories (scored in step 5)

GDAL's KML driver silently drops <SchemaData> attributes, returning 691 points
with nothing but a geometry, so the file is parsed directly.

Every point is enriched with the value of every factor at its location, using
exactly the same sampling path as the dashboard's click-to-inspect - which makes
this file a regression fixture for the API as well.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import rasterio
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.common import (OUT_DIR, RAW_DIR, VEC_DIR, banner, cog_path, load_registry, log,
                             write_json)

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}

# GSI ships 10-character shapefile column names. Restore them.
FIELD_RENAME = {
    "SLIDE_NO": "slide_no", "SLIDE_NAME": "slide_name", "DISTRICT": "district",
    "STATE": "state", "TOPOSHEET": "toposheet", "TRIGGERING": "trigger",
    "MATERIAL_T": "material_type", "MOVEMENT_T": "movement_type",
    "MOVEMENT_R": "movement_rate", "FAILURE_ME": "failure_mechanism",
    "ACTIVITY": "activity", "STYLE": "style", "DISTRIBUTI": "distribution",
    "GEOMORPHOL": "geomorphology", "HYDROLOGIC": "hydrology",
    "LANDUSE_LA": "landuse_at_slide", "LANDUSE_AF": "landuse_affected",
    "GEOLOGY": "geology", "STRUCTURE": "structure",
    "LENGTH": "length_m", "WIDTH": "width_m", "HEIGHT": "height_m", "DEPTH": "depth_m",
    "LS_AREA": "area_m2", "LS_VOLUME": "volume_m3", "RUNOUT_DIS": "runout_m",
    "NH_SH_LOCA": "road_class", "INFRASTRUC": "infrastructure_affected",
    "COMMUNICAT": "communication_affected", "PERSONS_DE": "persons_dead",
    "PEOPLE_AFF": "people_affected", "LIVESTOCK_": "livestock_affected",
    "PRE_REMEDI": "remediation", "GEOSCIENTI": "geoscientific_cause",
    "REMARKS": "remarks", "CITATION": "citation", "ABSTRACT": "abstract",
    "INITIATION": "initiation_year", "OBJECTID": "gsi_objectid",
}
NUMERIC = {"length_m", "width_m", "height_m", "depth_m", "area_m2", "volume_m3",
           "runout_m", "initiation_year", "gsi_objectid"}
DROP = {"fid", "Label", "LONGITUDE", "LATITUDE", "PHOTOS", "REPORT", "IMAGE_LAND",
        "REPORT_LAN", "REACTIVATI", "REACTIVA_1", "REACTIVA_2", "ALERT"}


def parse_kml(path: Path) -> list:
    """KML placemarks -> [{properties, lon, lat}]. GDAL's driver loses these attributes."""
    root = ET.parse(path).getroot()
    out = []
    for pm in root.iter(f"{{{KML_NS['k']}}}Placemark"):
        props = {}
        for sd in pm.iter(f"{{{KML_NS['k']}}}SimpleData"):
            name = sd.get("name")
            if name in DROP:
                continue
            val = (sd.text or "").strip()
            if val in ("", "NA", "N/A", "null", "-"):
                continue
            props[FIELD_RENAME.get(name, name.lower())] = val
        coord_el = pm.find(f".//{{{KML_NS['k']}}}coordinates")
        if coord_el is None or not coord_el.text:
            continue
        lon, lat, *_ = [float(v) for v in coord_el.text.strip().split(",")]
        for key in NUMERIC:
            if key in props:
                try:
                    f = float(props[key])
                    props[key] = int(f) if f.is_integer() else round(f, 3)
                except ValueError:
                    pass
        out.append({"properties": props, "lon": lon, "lat": lat})
    return out


def sample_layers(reg: dict, lons: np.ndarray, lats: np.ndarray) -> dict:
    """Value of every harmonised layer at each point (nearest pixel)."""
    import pyproj

    to_utm = pyproj.Transformer.from_crs("EPSG:4326", reg["project"]["crs"], always_xy=True).transform
    xs, ys = to_utm(lons, lats)
    out = {}
    for layer in reg["layers"]:
        lid = layer["id"]
        path = cog_path(lid)
        if not path.exists():
            continue
        s = layer["store"]
        with rasterio.open(path) as ds:
            rows, cols = rasterio.transform.rowcol(ds.transform, xs, ys)
            rows, cols = np.asarray(rows), np.asarray(cols)
            inside = (rows >= 0) & (rows < ds.height) & (cols >= 0) & (cols < ds.width)
            band = ds.read(1)
        vals = np.full(len(lons), np.nan)
        got = band[rows[inside], cols[inside]].astype("float64")
        got = np.where(got == s["nodata"], np.nan, got * s["scale"] + s["offset"])
        vals[inside] = got
        out[lid] = vals
    return out


def main() -> dict:
    banner("STEP 4  ·  GSI LANDSLIDE INVENTORY")
    reg = load_registry()
    vec_cfg = reg["vectors_by_id"]["inventory"]
    src = RAW_DIR / vec_cfg["file"]

    pts = parse_kml(src)
    log("parse", f"{len(pts):,} placemarks from {vec_cfg['file']}")
    attrs = sorted({k for p in pts for k in p["properties"]})
    log("parse", f"{len(attrs)} attributes: {', '.join(attrs[:12])} …")

    lons = np.array([p["lon"] for p in pts])
    lats = np.array([p["lat"] for p in pts])
    sampled = sample_layers(reg, lons, lats)
    cls = sampled.get("lsm_class", np.full(len(pts), np.nan))
    log("sample", f"factor values attached; {int(np.isfinite(cls).sum())}/{len(pts)} points "
                  f"fall inside the classified LSM extent")

    # ---- join to hotspot polygons -------------------------------------------
    hs = __import__("json").load(open(VEC_DIR / "hotspots.geojson"))
    geoms = [shape(f["geometry"]) for f in hs["features"]]
    ranks = [f["properties"]["rank_area"] for f in hs["features"]]
    tree = STRtree(geoms)
    slides_per_hotspot = {r: 0 for r in ranks}

    labels = {int(v): d["label"] for v, d in reg["layers_by_id"]["lsm_class"]["classes"].items()}
    feats = []
    for i, p in enumerate(pts):
        props = dict(p["properties"])
        factors = {}
        for lid, vals in sampled.items():
            v = vals[i]
            factors[lid] = None if not np.isfinite(v) else (
                int(v) if reg["layers_by_id"][lid]["kind"] == "categorical" else round(float(v), 4))
        props["factors"] = factors
        c = factors.get("lsm_class")
        props["risk_class"] = c
        props["risk_label"] = labels.get(c) if c else None
        props["susceptibility_index"] = factors.get("lsm")

        pt = Point(p["lon"], p["lat"])
        hit = None
        for j in tree.query(pt):
            if geoms[j].contains(pt):
                hit = ranks[j]
                break
        props["hotspot_rank"] = hit
        if hit is not None:
            slides_per_hotspot[hit] += 1

        feats.append({"type": "Feature", "id": i + 1, "properties": props,
                      "geometry": {"type": "Point",
                                   "coordinates": [round(p["lon"], 6), round(p["lat"], 6)]}})

    inside_hotspot = sum(1 for f in feats if f["properties"]["hotspot_rank"] is not None)
    log("join", f"{inside_hotspot}/{len(feats)} inventory points fall inside a Very-High hotspot "
                f"({100 * inside_hotspot / len(feats):.1f}%)")

    # push the count back onto the hotspot layers
    for fc_name in ("hotspots.geojson", "hotspot_points.geojson"):
        path = VEC_DIR / fc_name
        fc = __import__("json").load(open(path))
        for f in fc["features"]:
            f["properties"]["past_slides"] = slides_per_hotspot.get(f["properties"]["rank_area"], 0)
        write_json(path, fc)
    log("join", f"past_slides written back onto both hotspot layers "
                f"(max {max(slides_per_hotspot.values()) if slides_per_hotspot else 0} in one patch)")

    fc = {"type": "FeatureCollection", "features": feats}
    write_json(VEC_DIR / "inventory.geojson", fc)
    log("write", f"vector/inventory.geojson "
                 f"({(VEC_DIR / 'inventory.geojson').stat().st_size / 1e6:.2f} MB)")

    # ---- facets the dashboard filters on ------------------------------------
    facets = {}
    for key in ("district", "trigger", "material_type", "movement_type", "activity",
                "landuse_at_slide", "road_class"):
        counts = {}
        for f in feats:
            v = f["properties"].get(key)
            if v:
                counts[v] = counts.get(v, 0) + 1
        if counts:
            facets[key] = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    write_json(OUT_DIR / "inventory_facets.json", facets)
    for key, counts in facets.items():
        top = ", ".join(f"{k} ({v})" for k, v in list(counts.items())[:4])
        log("facet", f"{key:18s} {len(counts):>2d} values · {top}")

    return {"count": len(feats), "facets": facets, "inside_hotspot": inside_hotspot}


if __name__ == "__main__":
    main()
