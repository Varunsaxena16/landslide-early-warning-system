"""Build the Sikkim outline the map uses to isolate the study area.

The "Sikkim only" toggle needs a polygon of the area of interest so it can mask
everything outside it. That polygon is not taken from an atlas: it is OUR study
area, the valid footprint of the harmonised LULC raster, which is the same mask
every statistic in the dashboard is computed over. Using anything else would let
the outline and the numbers disagree at the edges.

Writes frontend/data/sikkim.geojson (WGS84). Run once to generate the result:

    .venv/bin/python tools/build_outline.py
"""
import json
import pathlib
import sys

import numpy as np
import pyproj
import rasterio
from rasterio import features
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "processed" / "cog" / "lulc.tif"
OUT = ROOT / "frontend" / "data" / "sikkim.geojson"

# Coarse enough to be a few kB rather than a few hundred; fine enough that the
# mask edge still reads as the state's own shape at the zooms the map allows.
SIMPLIFY_M = 120.0
# Drop the specks the raster edge leaves behind, in km2.
MIN_AREA_KM2 = 1.0


def main() -> None:
    if not SRC.exists():
        sys.exit(f"missing {SRC} — run `make data` first")

    with rasterio.open(SRC) as ds:
        band = ds.read(1)
        nodata = ds.nodata
        transform, crs = ds.transform, ds.crs

    valid = np.ones(band.shape, dtype="uint8")
    if nodata is not None:
        valid[band == nodata] = 0

    polys = [shape(g) for g, v in features.shapes(valid, mask=valid.astype(bool),
                                                  transform=transform, connectivity=8) if v == 1]
    merged = unary_union(polys)
    parts = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    parts = [p for p in parts if p.area / 1e6 >= MIN_AREA_KM2]
    parts.sort(key=lambda p: -p.area)

    outline = unary_union([p.simplify(SIMPLIFY_M, preserve_topology=True) for p in parts])
    to_wgs = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    outline = shp_transform(to_wgs, outline)
    outline = shp_transform(lambda x, y, z=None: (np.round(x, 5), np.round(y, 5)), outline)

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Sikkim study area"},
         "geometry": mapping(outline)}]}
    OUT.write_text(json.dumps(fc, separators=(",", ":")))

    km2 = sum(p.area for p in parts) / 1e6
    print(f"parts kept   {len(parts)}")
    print(f"area         {km2:,.1f} km2")
    print(f"bounds       {[round(v, 4) for v in outline.bounds]}")
    print(f"written      {OUT.relative_to(ROOT)}  ({OUT.stat().st_size / 1024:.1f} kB)")


if __name__ == "__main__":
    main()
