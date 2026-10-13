"""Build the two exposure COGs the hotspot score reads.

  WorldPop 2020 UN-adjusted people-per-pixel, India, 1 km
      -> data/processed/cog/worldpop.tif          people on building pixels

  Google Open Buildings v3 points, S2 cell 39f (covers Sikkim)
      -> data/processed/cog/open_buildings_count.tif
      -> data/processed/cog/open_buildings_area.tif

Sources land in data/interim/exposure_src/ (gitignored). The COGs are what
gets committed. Re-run this only when the sources or the analysis grid change.

    python tools/fetch_exposure.py
"""
from __future__ import annotations

import gzip
import sys
import urllib.request
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window, from_bounds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.common import cog_path, encode, load_grid, log, write_cog, write_json  # noqa: E402
from pipeline.exposure import (  # noqa: E402
    BUILDING_CONFIDENCE_MIN, BUILDINGS_AREA_COG, BUILDINGS_AREA_STORE,
    BUILDINGS_COUNT_COG, BUILDINGS_COUNT_STORE, OPEN_BUILDINGS_S2_TOKEN,
    OPEN_BUILDINGS_URL, WORLDPOP_COG, WORLDPOP_STORE, WORLDPOP_URL,
)

SRC_DIR = ROOT / "data" / "interim" / "exposure_src"
WORLDPOP_SRC = SRC_DIR / "ind_ppp_2020_1km_Aggregated_UNadj.tif"
BUILDINGS_SRC = SRC_DIR / f"{OPEN_BUILDINGS_S2_TOKEN}_buildings.csv.gz"
# Content-Length of the published files. A short file is a partial download.
WORLDPOP_BYTES = 18_313_124
BUILDINGS_BYTES = 2_305_651_866
OUT_DIR = ROOT / "data" / "processed"


def _download(url: str, dest: Path, expected: int) -> None:
    if dest.exists() and dest.stat().st_size == expected:
        log("fetch", f"have {dest.name} ({dest.stat().st_size / 1e9:.2f} GB)")
        return
    if dest.exists():
        log("fetch", f"{dest.name} is {dest.stat().st_size:,} bytes, expected {expected:,} — re-downloading")
    dest.parent.mkdir(parents=True, exist_ok=True)
    log("fetch", f"downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    log("fetch", f"wrote {dest.name} ({dest.stat().st_size / 1e9:.2f} GB)")


def _study_mask(grid) -> np.ndarray:
    """True on pixels the LULC raster treats as inside Sikkim."""
    with rasterio.open(cog_path("lulc")) as ds:
        lulc = ds.read(1)
        if (ds.width, ds.height) != (grid.width, grid.height):
            raise SystemExit("lulc COG is not on the analysis grid; run make data first")
        nodata = ds.nodata
    return (lulc != nodata) & (lulc != 0)


def build_worldpop(grid, study: np.ndarray, building_area: np.ndarray) -> dict:
    """Place each WorldPop cell's people onto the building pixels inside it.

    The source is a 1 km people-per-pixel grid. Spreading that count across
    every 30 m cell would mark bare slopes next to a village as inhabited.
    Instead the whole count of a 1 km cell is shared across the Open Buildings
    footprints that fall inside it, weighted by footprint area. A cell with
    people but no detected roof keeps its people unallocated: nothing there
    is exposed.
    """
    with rasterio.open(WORLDPOP_SRC) as src:
        west, south, east, north = transform_bounds(grid.crs, src.crs, *grid.bounds)
        pad = 0.02
        win = from_bounds(west - pad, south - pad, east + pad, north + pad, src.transform)
        win = win.intersection(Window(0, 0, src.width, src.height)).round_offsets().round_lengths()
        data = src.read(1, window=win).astype("float64")
        src_transform = src.window_transform(win)
        nodata = src.nodata
        src_crs = src.crs
    if nodata is not None:
        data[~np.isfinite(data) | np.isclose(data, nodata) | (data < 0)] = np.nan
    else:
        data[~np.isfinite(data) | (data < 0)] = np.nan

    flat = np.where(np.isfinite(data), data, 0.0).ravel()
    src_index = np.where(flat > 0, np.arange(flat.size), -1).reshape(data.shape).astype("int32")
    id_grid = np.full((grid.height, grid.width), -1, dtype="int32")
    reproject(
        source=src_index,
        destination=id_grid,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=grid.transform,
        dst_crs=grid.crs,
        resampling=Resampling.nearest,
        src_nodata=-1,
        dst_nodata=-1,
    )

    roofs = study & np.isfinite(building_area) & (building_area > 0) & (id_grid >= 0)
    people = np.full((grid.height, grid.width), np.nan, dtype="float64")
    people[study] = 0.0
    if roofs.any():
        ids = id_grid[roofs].astype("int64")
        weights = building_area[roofs].astype("float64")
        totals = np.bincount(ids, weights=weights)
        cell_people = flat[ids] * weights / totals[ids]
        people[roofs] = cell_people

    finite = people[np.isfinite(people)]
    peak = float(finite.max()) if finite.size else 0.0
    if peak > 1_000_000:
        raise SystemExit(f"a 30 m cell holds {peak:.0f} people, which is not a settlement")
    total = float(np.nansum(people))
    # People in source cells that touch the study area, before allocation.
    touched = id_grid[study & (id_grid >= 0)]
    source_total = float(flat[np.unique(touched)].sum()) if touched.size else 0.0
    write_cog(WORLDPOP_COG, encode(people, WORLDPOP_STORE), grid, WORLDPOP_STORE)
    log("worldpop", f"{total:,.0f} people allocated onto buildings "
                    f"({source_total:,.0f} in the 1 km cells that touch Sikkim)")
    return {
        "people_in_study_area": round(total, 1),
        "people_in_source_cells_touching_study_area": round(source_total, 1),
        "resolution": "1 km, allocated onto Open Buildings footprints",
    }


def build_buildings(grid, study: np.ndarray) -> tuple[dict, np.ndarray]:
    """Rasterise Open Buildings centroids that fall inside the analysis window."""
    west, south, east, north = transform_bounds(grid.crs, "EPSG:4326", *grid.bounds)
    pad = 0.01
    west, south, east, north = west - pad, south - pad, east + pad, north + pad

    count = np.zeros((grid.height, grid.width), dtype="float64")
    area = np.zeros_like(count)
    seen = kept = 0
    chunk_lon, chunk_lat, chunk_area = [], [], []

    def flush():
        nonlocal chunk_lon, chunk_lat, chunk_area
        if not chunk_lon:
            return
        import pyproj
        to_utm = pyproj.Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True).transform
        xs, ys = to_utm(chunk_lon, chunk_lat)
        xs = np.asarray(xs)
        ys = np.asarray(ys)
        cols = np.floor((xs - grid.transform.c) / grid.transform.a).astype("int64")
        rows = np.floor((grid.transform.f - ys) / (-grid.transform.e)).astype("int64")
        ok = (rows >= 0) & (rows < grid.height) & (cols >= 0) & (cols < grid.width)
        if ok.any():
            np.add.at(count, (rows[ok], cols[ok]), 1.0)
            np.add.at(area, (rows[ok], cols[ok]), np.asarray(chunk_area, dtype="float64")[ok])
        chunk_lon, chunk_lat, chunk_area = [], [], []

    log("buildings", f"scanning {BUILDINGS_SRC.name} for {south:.2f}–{north:.2f}°N, "
                     f"{west:.2f}–{east:.2f}°E, confidence ≥ {BUILDING_CONFIDENCE_MIN}")
    # Sikkim sits in a thin latitude band of a cell that also covers the plains.
    # Reject the other rows before the CSV parser touches them.
    with gzip.open(BUILDINGS_SRC, "rt", newline="") as fh:
        for line in fh:
            seen += 1
            if seen % 5_000_000 == 0:
                log("buildings", f"{seen / 1e6:.0f} M rows, {kept:,} inside Sikkim")
                flush()
            if not (line.startswith("27.") or line.startswith("28.")):
                continue
            parts = line.split(",", 4)
            if len(parts) < 4:
                continue
            try:
                lat = float(parts[0])
                lon = float(parts[1])
                footprint = float(parts[2])
                confidence = float(parts[3])
            except ValueError:
                continue
            if not (south <= lat <= north and west <= lon <= east):
                continue
            if confidence < BUILDING_CONFIDENCE_MIN or footprint <= 0:
                continue
            chunk_lon.append(lon)
            chunk_lat.append(lat)
            chunk_area.append(footprint)
            kept += 1
            if len(chunk_lon) >= 250_000:
                flush()
    flush()

    count = np.where(study, count, np.nan)
    area = np.where(study, area, np.nan)
    n = int(np.nansum(count))
    m2 = float(np.nansum(area))
    write_cog(BUILDINGS_COUNT_COG, encode(count, BUILDINGS_COUNT_STORE), grid, BUILDINGS_COUNT_STORE)
    write_cog(BUILDINGS_AREA_COG, encode(area, BUILDINGS_AREA_STORE), grid, BUILDINGS_AREA_STORE)
    log("buildings", f"{n:,} footprints, {m2 / 1e6:.2f} km²  "
                     f"(scanned {seen:,} rows in cell {OPEN_BUILDINGS_S2_TOKEN})")
    return {
        "rows_scanned": seen,
        "buildings_in_study_area": n,
        "footprint_m2": round(m2, 1),
        "confidence_min": BUILDING_CONFIDENCE_MIN,
    }, np.where(np.isfinite(area), area, 0.0)


def main() -> None:
    _download(WORLDPOP_URL, WORLDPOP_SRC, WORLDPOP_BYTES)
    _download(OPEN_BUILDINGS_URL, BUILDINGS_SRC, BUILDINGS_BYTES)
    grid = load_grid()
    study = _study_mask(grid)
    bld, building_area = build_buildings(grid, study)
    pop = build_worldpop(grid, study, building_area)
    meta = {
        "worldpop": {
            "title": "WorldPop 2020 UN-adjusted, unconstrained, 1 km, allocated onto Open Buildings",
            "url": WORLDPOP_URL,
            "citation": "WorldPop (www.worldpop.org) and Center for International Earth "
                        "Science Information Network (CIESIN), Columbia University (2018). "
                        "Global High Resolution Population Denominators Project. "
                        "Funded by The Bill and Melinda Gates Foundation (OPP1134076).",
            **pop,
        },
        "open_buildings": {
            "title": "Google Open Buildings v3, centroids",
            "url": OPEN_BUILDINGS_URL,
            "s2_token": OPEN_BUILDINGS_S2_TOKEN,
            "citation": "Sirko, W., et al. (2021). Continental-scale building detection "
                        "from high resolution satellite imagery. "
                        "https://sites.research.google/open-buildings/",
            **bld,
        },
    }
    write_json(OUT_DIR / "exposure_sources.json", meta)
    log("write", "data/processed/exposure_sources.json")


if __name__ == "__main__":
    main()
