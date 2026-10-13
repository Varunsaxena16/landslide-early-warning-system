"""Point query - the click-to-inspect behind panel one.

Returns the value of every conditioning factor at one coordinate plus the
model's verdict there, which is exactly what was asked for in the meeting
(16:35:01: "clicking a point returns the values of the 17 variables").

Reads a 1x1 window out of each COG. That is a handful of disk seeks, not a
raster load, so it stays fast with any number of layers.
"""
from __future__ import annotations

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

from backend import registry


def _decode(stored, store):
    if stored is None or stored == store["nodata"]:
        return None
    return float(stored) * store["scale"] + store["offset"]


def sample_point(lon: float, lat: float) -> dict:
    reg = registry.registry()
    crs = reg["project"]["crs"]
    xs, ys = warp_transform("EPSG:4326", crs, [lon], [lat])
    x, y = xs[0], ys[0]

    values, inside_any = [], False
    for lyr in reg["layers"]:
        entry = {"id": lyr["id"], "title": lyr["title"], "group": lyr["group"],
                 "group_label": lyr["group_label"], "kind": lyr["kind"],
                 "units": lyr.get("units"), "value": None, "label": None,
                 "display": lyr.get("display", {})}
        with rasterio.open(lyr["path"]) as ds:
            row, col = ds.index(x, y)
            if 0 <= row < ds.height and 0 <= col < ds.width:
                inside_any = True
                win = rasterio.windows.Window(col, row, 1, 1)
                raw = ds.read(1, window=win)[0, 0]
                val = _decode(raw, lyr["store"])
                if val is not None:
                    if lyr["kind"] == "categorical":
                        entry["value"] = int(val)
                        entry["label"] = registry.class_labels(lyr["id"]).get(int(val))
                        cls = next((c for c in lyr["classes"] if c["value"] == int(val)), None)
                        entry["color"] = cls["color"] if cls else None
                        entry["inferred"] = bool(cls and cls.get("inferred"))
                    else:
                        entry["value"] = round(val, 4)
        values.append(entry)

    by_id = {v["id"]: v for v in values}
    risk_class = by_id.get("lsm_class", {}).get("value")
    index = by_id.get("lsm", {}).get("value")

    # normalised 0-1 position of this pixel within the observed index range,
    # so the UI can draw a gauge without hard-coding the range
    cls_info = reg["classification"]
    imin = cls_info.get("index_min", 0.0)
    imax = cls_info.get("index_max", 1.0)
    norm = None if index is None else round((index - imin) / max(imax - imin, 1e-9), 4)

    return {
        "lon": round(lon, 6), "lat": round(lat, 6),
        "utm": {"crs": crs, "x": round(x, 1), "y": round(y, 1)},
        "inside_study_area": by_id.get("lulc", {}).get("value") is not None,
        "inside_grid": inside_any,
        "risk": {
            "class": risk_class,
            "label": by_id.get("lsm_class", {}).get("label"),
            "color": by_id.get("lsm_class", {}).get("color"),
            "index": index,
            "index_normalised": norm,
            "breaks": cls_info.get("breaks"),
        },
        "factors": values,
    }


def sample_profile(lon1, lat1, lon2, lat2, n: int = 64) -> dict:
    """Susceptibility along a straight line - useful for reading a road corridor."""
    lons = np.linspace(lon1, lon2, n)
    lats = np.linspace(lat1, lat2, n)
    lyr = registry.layer("lsm")
    cls_lyr = registry.layer("lsm_class")
    out = []
    with rasterio.open(lyr["path"]) as ds, rasterio.open(cls_lyr["path"]) as cds:
        xs, ys = warp_transform("EPSG:4326", ds.crs.to_string(), list(lons), list(lats))
        band, cband = ds.read(1), cds.read(1)
        for i, (x, y) in enumerate(zip(xs, ys)):
            r, c = ds.index(x, y)
            v = cl = None
            if 0 <= r < ds.height and 0 <= c < ds.width:
                v = _decode(band[r, c], lyr["store"])
                cl = _decode(cband[r, c], cls_lyr["store"])
            out.append({"t": round(i / (n - 1), 4), "lon": round(lons[i], 6),
                        "lat": round(lats[i], 6),
                        "index": None if v is None else round(v, 4),
                        "class": None if cl is None else int(cl)})
    return {"samples": out}
