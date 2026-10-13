"""In-memory feature store for the two vector layers.

Both files are loaded once at start-up and indexed with an STRtree, so a bbox
query while the user pans is a spatial-index lookup rather than a scan of
10,000 polygons.
"""
from __future__ import annotations

import json
from functools import lru_cache

from shapely.geometry import box, shape
from shapely.strtree import STRtree

from backend import registry


class FeatureStore:
    def __init__(self, path):
        self.features = []
        self.geoms = []
        self.tree = None
        if path.exists():
            with open(path) as fh:
                fc = json.load(fh)
            self.features = fc.get("features", [])
            self.geoms = [shape(f["geometry"]) for f in self.features]
            if self.geoms:
                self.tree = STRtree(self.geoms)

    def __len__(self):
        return len(self.features)

    def query(self, bbox=None, limit=None, order_by=None, descending=True,
              predicate=None, offset=0):
        if bbox and self.tree is not None:
            idxs = list(self.tree.query(box(*bbox)))
        else:
            idxs = range(len(self.features))

        feats = [self.features[i] for i in idxs]
        if predicate:
            feats = [f for f in feats if predicate(f["properties"])]
        if order_by:
            feats = sorted(feats, key=lambda f: (f["properties"].get(order_by) is None,
                                                 f["properties"].get(order_by) or 0),
                           reverse=descending)
        total = len(feats)
        feats = feats[offset:] if offset else feats
        if limit:
            feats = feats[:limit]
        return feats, total

    def by_id(self, fid):
        for f in self.features:
            if f.get("id") == fid:
                return f
        return None


@lru_cache(maxsize=1)
def hotspots() -> FeatureStore:
    return FeatureStore(registry.vector_path("hotspots"))


@lru_cache(maxsize=1)
def hotspot_points() -> FeatureStore:
    return FeatureStore(registry.vector_path("hotspot_points"))


@lru_cache(maxsize=1)
def inventory() -> FeatureStore:
    return FeatureStore(registry.vector_path("inventory"))


def strip(feature: dict, drop=("citation", "abstract", "factors")) -> dict:
    """Trim the bulky attributes for list responses."""
    props = {k: v for k, v in feature["properties"].items() if k not in drop}
    return {**feature, "properties": props}
