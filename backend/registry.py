"""Loads config/layers.yml plus the pipeline's outputs into one catalogue.

Everything the frontend needs to draw the sidebar, the legends and the
inspector comes from here, so adding a factor raster never means touching
JavaScript.
"""
from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path

import yaml

from backend.config import COG_DIR, CONFIG_FILE, PLACES_FILE, PROCESSED_DIR, VEC_DIR

GROUP_ORDER = ["model", "warning", "terrain", "hydrology", "landcover", "geology", "climate", "anthropogenic"]
GROUP_LABELS = {
    "model": "Model output",
    "warning": "Live surfaces",
    "terrain": "Terrain",
    "hydrology": "Hydrology",
    "landcover": "Land cover",
    "geology": "Geology & soil",
    "climate": "Climate",
    "anthropogenic": "Human footprint",
}


def _load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path) as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def registry() -> dict:
    with open(CONFIG_FILE) as fh:
        cfg = yaml.safe_load(fh)

    classification = _load_json(PROCESSED_DIR / "classification.json", {})
    harmonise = _load_json(PROCESSED_DIR / "harmonise.json", {})
    grid = _load_json(PROCESSED_DIR / "grid.json", {})

    layers = []
    for lyr in cfg["layers"]:
        lid = lyr["id"]
        path = COG_DIR / f"{lid}.tif"
        if not path.exists():
            continue
        h = harmonise.get("layers", {}).get(lid, {})
        entry = {
            "id": lid,
            "title": lyr["title"],
            "group": lyr.get("group", "other"),
            "group_label": GROUP_LABELS.get(lyr.get("group", "other"), "Other"),
            "kind": lyr["kind"],
            "units": lyr.get("units"),
            "source": lyr.get("source"),
            "role": lyr.get("role"),
            "notes": lyr.get("notes"),
            "labels_inferred": bool(lyr.get("labels_inferred")),
            "display": lyr.get("display", {}),
            "store": lyr["store"],
            "coverage": h.get("coverage_of_study_area"),
            "stats": h.get("stats"),
            "path": str(path),
        }
        if lyr["kind"] == "categorical":
            entry["classes"] = [
                {"value": int(v), "label": d["label"], "color": d["color"],
                 "inferred": bool(d.get("inferred")),
                 "area_km2": (h.get("class_area_km2") or {}).get(str(v))
                             or (h.get("class_area_km2") or {}).get(v)}
                for v, d in sorted(lyr["classes"].items(), key=lambda kv: int(kv[0]))
            ]
        layers.append(entry)

    layers.sort(key=lambda l: (GROUP_ORDER.index(l["group"]) if l["group"] in GROUP_ORDER else 99,
                               l["title"]))

    return {
        "project": cfg["project"],
        "layers": layers,
        "layers_by_id": {l["id"]: l for l in layers},
        "vectors": cfg["vectors"],
        "classification": classification,
        "grid": grid,
        "harmonise": harmonise,
    }


@lru_cache(maxsize=1)
def places() -> list:
    if not PLACES_FILE.exists():
        return []
    with open(PLACES_FILE) as fh:
        rows = list(csv.DictReader(fh))
    return [{"name": r["name"], "type": r["type"], "district": r["district"],
             "lat": float(r["lat"]), "lon": float(r["lon"]), "note": r.get("note") or None}
            for r in rows]


@lru_cache(maxsize=1)
def data_version() -> str:
    """A short token that changes whenever the pipeline is re-run.

    Tiles are cached for a day, so without this a client keeps serving stale
    imagery after `make data` — which is exactly what happened when the soil
    raster was clipped to the state and browsers went on drawing the old
    rectangle. The frontend appends it to every tile URL, so new data means a
    new URL and the cache is bypassed for free.
    """
    newest = 0.0
    paths = [CONFIG_FILE]
    for name in ("stats.json", "classification.json", "harmonise.json",
                 "risk/live_surfaces.json"):
        paths.append(PROCESSED_DIR / name)
    for path in paths:
        if path.exists():
            newest = max(newest, path.stat().st_mtime)
    return format(int(newest), "x")


@lru_cache(maxsize=1)
def stats() -> dict:
    return _load_json(PROCESSED_DIR / "stats.json", {}) or {}


@lru_cache(maxsize=1)
def facets() -> dict:
    return _load_json(PROCESSED_DIR / "inventory_facets.json", {}) or {}


def layer(layer_id: str) -> dict | None:
    return registry()["layers_by_id"].get(layer_id)


def class_labels(layer_id: str) -> dict:
    lyr = layer(layer_id)
    if not lyr or lyr["kind"] != "categorical":
        return {}
    return {c["value"]: c["label"] for c in lyr["classes"]}


def vector_path(name: str) -> Path:
    return VEC_DIR / f"{name}.geojson"
