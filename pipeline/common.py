"""Shared plumbing for the pipeline: paths, registry, the analysis grid, COG writing."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil
import yaml
from scipy import ndimage
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = Path(os.environ.get("RAW_DIR", ROOT / "Risk_hotspots"))
OUT_DIR = Path(os.environ.get("PROCESSED_DIR", ROOT / "data" / "processed"))
COG_DIR = OUT_DIR / "cog"
VEC_DIR = OUT_DIR / "vector"
CONFIG = ROOT / "config" / "layers.yml"

RESAMPLING = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    "average": Resampling.average,
    "mode": Resampling.mode,
}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def load_registry() -> dict:
    with open(CONFIG) as fh:
        reg = yaml.safe_load(fh)
    reg["layers_by_id"] = {lyr["id"]: lyr for lyr in reg["layers"]}
    reg["vectors_by_id"] = {v["id"]: v for v in reg["vectors"]}
    return reg


def raw_path(layer: dict) -> Path:
    return RAW_DIR / layer["file"]


def cog_path(layer_id: str) -> Path:
    return COG_DIR / f"{layer_id}.tif"


def source_layers(reg: dict) -> list:
    """Layers that are read from a raw file (i.e. not produced by the pipeline)."""
    return [l for l in reg["layers"] if not l.get("derived")]


# --------------------------------------------------------------------------- #
# the analysis grid
# --------------------------------------------------------------------------- #
@dataclass
class Grid:
    """The single grid every raster is resampled onto.

    Snapped to the LSM ensemble's own pixel origin so that the model output -
    the one layer we must not smear - lands on an exact integer pixel offset
    and is copied rather than interpolated.
    """

    crs: CRS
    transform: Affine
    width: int
    height: int

    @property
    def res(self) -> float:
        return self.transform.a

    @property
    def bounds(self):
        return rasterio.transform.array_bounds(self.height, self.width, self.transform)

    def profile(self, dtype, nodata) -> dict:
        return dict(
            driver="GTiff",
            dtype=dtype,
            nodata=nodata,
            width=self.width,
            height=self.height,
            count=1,
            crs=self.crs,
            transform=self.transform,
        )

    def to_json(self) -> dict:
        from rasterio.warp import transform_bounds

        b = self.bounds
        wgs = transform_bounds(self.crs, "EPSG:4326", *b)
        return {
            "crs": str(self.crs),
            "resolution": self.res,
            "width": self.width,
            "height": self.height,
            "transform": list(self.transform)[:6],
            "bounds_utm": [round(v, 3) for v in b],
            "bounds_wgs84": [round(v, 6) for v in wgs],
            "center_wgs84": [round((wgs[0] + wgs[2]) / 2, 6), round((wgs[1] + wgs[3]) / 2, 6)],
        }


def load_grid() -> Grid:
    """Read the analysis grid back from data/processed/grid.json."""
    with open(OUT_DIR / "grid.json") as fh:
        g = json.load(fh)
    t = g["transform"]
    return Grid(crs=CRS.from_string(g["crs"]), transform=Affine(*t),
                width=g["width"], height=g["height"])


def resolve_grid(reg: dict) -> Grid:
    """The grid, without needing the raw delivery.

    Once step 2 has run, grid.json is the published definition and nothing
    downstream should reach back to the 881 MB of source rasters just to
    recover a pixel origin. Steps 3-5, the tests and CI all take this path;
    only step 2 itself derives the grid from the raw files.
    """
    if (OUT_DIR / "grid.json").exists():
        return load_grid()
    return build_grid(reg)


def build_grid(reg: dict) -> Grid:
    """Union of every source layer's extent, at the project resolution, snapped to the LSM grid."""
    res = float(reg["project"]["resolution"])
    crs = CRS.from_string(reg["project"]["crs"])

    with rasterio.open(raw_path(reg["layers_by_id"]["lsm"])) as ds:
        anchor_x, anchor_y = ds.transform.c, ds.transform.f

    from rasterio.warp import transform_bounds

    xs, ys = [], []
    for lyr in source_layers(reg):
        with rasterio.open(raw_path(lyr)) as ds:
            b = ds.bounds if ds.crs == crs else transform_bounds(ds.crs, crs, *ds.bounds)
            # The rainfall raster is a coarse 5 km climatology whose footprint spills
            # well beyond Sikkim; it must not be allowed to inflate the grid.
            if lyr["id"] == "rainfall":
                continue
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]

    # snap outward to the LSM pixel lattice
    left = anchor_x + np.floor((min(xs) - anchor_x) / res) * res
    right = anchor_x + np.ceil((max(xs) - anchor_x) / res) * res
    top = anchor_y + np.ceil((max(ys) - anchor_y) / res) * res
    bottom = anchor_y + np.floor((min(ys) - anchor_y) / res) * res

    width = int(round((right - left) / res))
    height = int(round((top - bottom) / res))
    return Grid(crs=crs, transform=Affine(res, 0, left, 0, -res, top), width=width, height=height)


# --------------------------------------------------------------------------- #
# reading a source raster onto the grid
# --------------------------------------------------------------------------- #
def read_onto_grid(layer: dict, grid: Grid) -> np.ndarray:
    """Warp one source raster onto the analysis grid, returning float64 with NaN for no-data."""
    path = raw_path(layer)
    resamp = RESAMPLING[layer.get("resampling", "bilinear")]

    with rasterio.open(path) as src:
        nodata_in = layer.get("nodata_in", None)
        if nodata_in is None:
            nodata_in = src.nodata
        with WarpedVRT(
            src,
            crs=grid.crs,
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=resamp,
            src_nodata=nodata_in,
            nodata=np.nan if src.dtypes[0].startswith("float") else nodata_in,
            dtype="float64",
        ) as vrt:
            arr = vrt.read(1).astype("float64")

    if nodata_in is not None:
        if abs(float(nodata_in)) > 1e29:          # float32 sentinel like -3.4e38
            arr[arr < -1e29] = np.nan
        else:
            arr[np.isclose(arr, float(nodata_in))] = np.nan
    # Guard against sentinels the resampler smeared into neighbouring pixels.
    if nodata_in is not None and float(nodata_in) < -1000:
        arr[arr < float(nodata_in) / 2] = np.nan
    if layer.get("zero_is_nodata"):
        arr[arr == 0] = np.nan
    arr[~np.isfinite(arr)] = np.nan

    # Coarse climatologies (CHIRPS is ~5 km) stop short of the study-area edge once
    # they are bilinearly resampled to 30 m. Backfill only those edge pixels from a
    # nearest-neighbour pass so a click near the border still returns a value.
    if layer.get("edge_fill"):
        holes = ~np.isfinite(arr)
        if holes.any():
            with rasterio.open(path) as src:
                with WarpedVRT(src, crs=grid.crs, transform=grid.transform,
                               width=grid.width, height=grid.height,
                               resampling=Resampling.nearest, src_nodata=nodata_in,
                               dtype="float64") as vrt:
                    near = vrt.read(1).astype("float64")
            if nodata_in is not None:
                near[np.isclose(near, float(nodata_in))] = np.nan
            arr[holes] = near[holes]
    return arr


def extend_to_study(arr: np.ndarray, study: np.ndarray, nodata=None) -> np.ndarray:
    """Continue the nearest valid value out to the study-area outline.

    The ensemble raster, and anything clipped to it, stops a few kilometres
    short of the southern and eastern outline. Those pixels are still inside
    Sikkim, so a layer that leaves them empty draws a hard line inside the
    state. Only holes that touch the outside of the study area are filled;
    a gap in the interior is left empty, and nothing is painted past the mask.
    """
    if np.issubdtype(arr.dtype, np.floating):
        valid = np.isfinite(arr)
    else:
        if nodata is None:
            raise ValueError("nodata is required for an integer raster")
        valid = arr != nodata
    holes = np.asarray(study, dtype=bool) & ~valid
    if not holes.any() or not valid.any():
        return arr
    struct = ndimage.generate_binary_structure(2, 2)
    edge = ndimage.binary_dilation(~study, structure=struct) & holes
    if not edge.any():
        return arr
    labels, _ = ndimage.label(holes, structure=struct)
    keep = np.unique(labels[edge])
    keep = keep[keep != 0]
    fill = np.isin(labels, keep)
    _, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    out = arr.copy()
    out[fill] = arr[iy[fill], ix[fill]]
    return out


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def encode(arr: np.ndarray, store: dict) -> np.ndarray:
    """real values (NaN = no-data) -> the stored integer representation."""
    dtype, scale, offset, nodata = store["dtype"], store["scale"], store["offset"], store["nodata"]
    out = np.full(arr.shape, nodata, dtype=dtype)
    ok = np.isfinite(arr)
    if ok.any():
        info = np.iinfo(dtype)
        vals = np.rint((arr[ok] - offset) / scale)
        vals = np.clip(vals, info.min, info.max)
        out[ok] = vals.astype(dtype)
    return out


def write_cog(path: Path, arr: np.ndarray, grid: Grid, store: dict, overviews=(2, 4, 8, 16, 32)):
    """Write a tiled, overview-backed, DEFLATE-compressed COG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = grid.profile(store["dtype"], store["nodata"])
    profile.update(
        tiled=True,
        blockxsize=512,
        blockysize=512,
        compress="deflate",
        predictor=2,
        zlevel=9,
        BIGTIFF="IF_SAFER",
        interleave="band",
    )
    tmp = path.with_suffix(".tmp.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(arr, 1)
        dst.build_overviews(list(overviews), Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")
    # COPY_SRC_OVERVIEWS produces a properly laid-out cloud-optimised file
    with rasterio.open(tmp) as src:
        rasterio.shutil.copy(
            src, path, copy_src_overviews=True, driver="GTiff", tiled=True,
            blockxsize=512, blockysize=512, compress="deflate", predictor=2,
            zlevel=9, BIGTIFF="IF_SAFER",
        )
    tmp.unlink()
    return path


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, default=_json_default)
    return path


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def log(step: str, msg: str):
    print(f"  [{step}] {msg}", flush=True)


def banner(title: str):
    print(f"\n\033[1m{'=' * 78}\n{title}\n{'=' * 78}\033[0m", flush=True)
