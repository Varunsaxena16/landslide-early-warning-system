"""Exposure and risk for Very-High patches.

Risk is susceptibility times exposure. Exposure is people and buildings, not
patch area and not distance to a road: a bare slope with nobody on it is not
an exposure hotspot, however high the ensemble index.

  population   WorldPop 2020, UN-adjusted, people per 30 m pixel
  buildings    Google Open Buildings v3 footprints (count and area)
  exposure     equal-weight log share of those two, in [0, 1]
               exactly 0 when a patch has no people and no buildings
  risk         mean susceptibility index × exposure

Both published scores are on a 0–100 scale:

  exposure_score = 100 × exposure
  risk_score     = mean_index × exposure_score
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio

from pipeline.common import COG_DIR

WORLDPOP_COG = COG_DIR / "worldpop.tif"
BUILDINGS_COUNT_COG = COG_DIR / "open_buildings_count.tif"
BUILDINGS_AREA_COG = COG_DIR / "open_buildings_area.tif"

# People on a 30 m pixel. A whole 1 km cell can land on one roof, so the
# ceiling has to sit in the tens of thousands, not the hundreds.
WORLDPOP_STORE = {"dtype": "uint32", "scale": 0.01, "offset": 0, "nodata": 4294967295}
BUILDINGS_COUNT_STORE = {"dtype": "uint16", "scale": 1, "offset": 0, "nodata": 65535}
# Footprint area, m², summed onto the pixel that holds the building centroid.
BUILDINGS_AREA_STORE = {"dtype": "uint16", "scale": 1, "offset": 0, "nodata": 65535}

POPULATION_WEIGHT = 0.5
BUILDING_WEIGHT = 0.5
# Google's own guidance: detections below ~0.65 are the weak tail.
BUILDING_CONFIDENCE_MIN = 0.65

# S2 level-4 cell that covers the Sikkim analysis grid (22.5–28.7°N, 84.9–90°E).
OPEN_BUILDINGS_S2_TOKEN = "39f"
# 1 km, not 100 m. The 100 m GeoTIFF is 1.65 GB on a host that ignores HTTP
# range requests, so a Sikkim window cannot be fetched on its own. The 1 km
# UN-adjusted grid is the WorldPop product that can actually be built here.
# People are dasymetrically placed onto Open Buildings centroids in
# tools/fetch_exposure.py, which is what keeps a bare slope inside a populated
# 1 km cell from inheriting that cell's headcount.
WORLDPOP_URL = (
    "https://data.worldpop.org/GIS/Population/Global_2000_2020_1km_UNadj/2020/IND/"
    "ind_ppp_2020_1km_Aggregated_UNadj.tif"
)
OPEN_BUILDINGS_URL = (
    "https://storage.googleapis.com/open-buildings-data/v3/"
    f"points_s2_level_4_gzip/{OPEN_BUILDINGS_S2_TOKEN}_buildings.csv.gz"
)


def combine_exposure(population: np.ndarray, building_area_m2: np.ndarray,
                     pop_weight: float = POPULATION_WEIGHT,
                     building_weight: float = BUILDING_WEIGHT) -> np.ndarray:
    """Exposure in [0, 1]. Zero iff both inputs are zero.

    Log1p, scaled by the maximum in this set, so one dense town does not pin
    every other patch to ~0 the way a raw max-normalisation would, and so the
    least-occupied *non-empty* patch stays strictly above zero. Percentile
    rank would have assigned that patch 0 and the zero-exposure filter would
    have deleted it.
    """
    pop = np.clip(np.asarray(population, dtype="float64"), 0, None)
    area = np.clip(np.asarray(building_area_m2, dtype="float64"), 0, None)
    pop_max = float(pop.max()) if pop.size else 0.0
    area_max = float(area.max()) if area.size else 0.0
    pop_term = np.log1p(pop) / np.log1p(pop_max) if pop_max > 0 else np.zeros_like(pop)
    area_term = np.log1p(area) / np.log1p(area_max) if area_max > 0 else np.zeros_like(area)
    if pop_max > 0 and area_max > 0:
        exposure = pop_weight * pop_term + building_weight * area_term
    elif pop_max > 0:
        exposure = pop_term
    elif area_max > 0:
        exposure = area_term
    else:
        exposure = np.zeros_like(pop)
    return np.where((pop <= 0) & (area <= 0), 0.0, exposure)


def publish_scores(mean_index: float, exposure_01: float) -> tuple[float, float]:
    """Round to the numbers the API and the dashboard actually show.

    risk_score is the product of those published numbers, so a reader can check
    risk = susceptibility × exposure without recovering the unrounded terms.
    A non-zero exposure that would round to 0.0 is kept at 0.1 so the
    zero-exposure filter and the published score cannot disagree.
    """
    exposure_score = round(100.0 * float(exposure_01), 1)
    if exposure_01 > 0 and exposure_score <= 0:
        exposure_score = 0.1
    risk_score = round(float(mean_index) * exposure_score, 1)
    if exposure_score > 0 and mean_index > 0 and risk_score <= 0:
        risk_score = 0.1
    return exposure_score, risk_score


def has_exposure(population: float, building_count: float) -> bool:
    return float(population) > 0 or float(building_count) > 0


def _read_stored(path: Path, store: dict) -> np.ndarray:
    with rasterio.open(path) as ds:
        stored = ds.read(1)
    return np.where(stored == store["nodata"], np.nan,
                    stored.astype("float64") * store["scale"] + store["offset"])


def load_exposure_rasters() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """People, building count, building footprint m², aligned to the analysis grid."""
    missing = [p.name for p in (WORLDPOP_COG, BUILDINGS_COUNT_COG, BUILDINGS_AREA_COG)
               if not p.exists()]
    if missing:
        raise SystemExit(
            "exposure rasters missing: " + ", ".join(missing) + "\n"
            "Run:  python tools/fetch_exposure.py"
        )
    return (_read_stored(WORLDPOP_COG, WORLDPOP_STORE),
            _read_stored(BUILDINGS_COUNT_COG, BUILDINGS_COUNT_STORE),
            _read_stored(BUILDINGS_AREA_COG, BUILDINGS_AREA_STORE))


FORMULA = (
    "exposure = 0.5*log1p(worldpop_people)/log1p(max) "
    "+ 0.5*log1p(open_buildings_m2)/log1p(max); "
    "risk_score = mean_susceptibility_index * exposure_score; "
    "patches with no WorldPop population and no Open Buildings footprint are dropped"
)
