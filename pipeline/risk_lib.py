"""Approach 1 is the published map. Approach 2 is evaluation only.

Approach 1: risk = susceptibility × exposure(people, buildings).
Approach 2: the same product with a rainfall term folded into exposure.
            That is a time trigger, not a map that can be checked against GSI.

Rainfall is never written onto /api/hotspots from this module.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np

POP_SUM_BAND = (400_000.0, 900_000.0)
PIXEL_M_BAND = (50.0, 250.0)
BUILDING_COUNT_BAND = (50_000, 800_000)
BUILDING_CONFIDENCE = (0.65, 1.0)
BUILDING_AREA_M2 = (1.0, 50_000.0)
OVERLAP_REGRESSION_PP = 5.0
DEG_PER_METRE = 1.0 / 111_320.0


class GateError(ValueError):
    """A sanity gate failed. Callers must not write production GeoJSON."""


def keep_risk_hotspot(population: float, buildings: int) -> bool:
    """A patch is societal risk only if someone or some roof is on it."""
    return int(buildings) > 0 or float(population) >= 10.0


def p95_positive(values: Iterable[float]) -> float:
    arr = np.asarray(list(values), dtype="float64")
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if arr.size == 0:
        raise GateError("P95 is undefined: no positive values")
    return float(np.percentile(arr, 95))


def _share(value: float, cap: float) -> float:
    if cap <= 0:
        raise GateError("P95 cap must be > 0")
    if not np.isfinite(value) or value < 0:
        raise GateError(f"exposure input is not a finite ≥ 0 number: {value}")
    return float(min(value / cap, 1.0))


def exposure_approach1(population: float, buildings: float,
                       p95_pop: float, p95_buildings: float) -> float:
    """Locked Approach 1 exposure in [0, 1]."""
    exp = 0.5 * _share(population, p95_pop) + 0.5 * _share(buildings, p95_buildings)
    if not 0.0 <= exp <= 1.0:
        raise GateError(f"exposure left [0, 1]: {exp}")
    return exp


def exposure_approach2(population: float, buildings: float, rainfall: float,
                       p95_pop: float, p95_buildings: float, p95_rain: float) -> float:
    """Approach 2 — evaluation only. Rainfall is a third equal share."""
    exp = (
        _share(population, p95_pop)
        + _share(buildings, p95_buildings)
        + _share(rainfall, p95_rain)
    ) / 3.0
    if not 0.0 <= exp <= 1.0:
        raise GateError(f"approach-2 exposure left [0, 1]: {exp}")
    return exp


def risk_score(susceptibility: float, exposure: float) -> float:
    if not np.isfinite(susceptibility) or not (0.0 < susceptibility <= 1.0):
        raise GateError(f"susceptibility must be in (0, 1]: {susceptibility}")
    if not np.isfinite(exposure) or not (0.0 <= exposure <= 1.0):
        raise GateError(f"exposure must be in [0, 1]: {exposure}")
    risk = float(susceptibility) * float(exposure)
    if abs(risk - susceptibility * exposure) > 1e-6:
        raise GateError("risk is not the product of susceptibility and exposure")
    return risk


def publish_aliases(population: float, buildings: float) -> dict:
    pop = int(round(float(population)))
    bld = int(round(float(buildings)))
    if pop < 0 or bld < 0:
        raise GateError("Population_Exposed and Buildings_At_Risk must be ≥ 0")
    return {"Population_Exposed": pop, "Buildings_At_Risk": bld}


def zonal_sum(grid: np.ndarray, mask: np.ndarray, nodata=None) -> float:
    """Sum finite, non-negative cells inside mask. Nodata and negatives are out."""
    arr = np.asarray(grid, dtype="float64")
    keep = np.asarray(mask, dtype=bool)
    if nodata is not None:
        keep = keep & (arr != nodata)
    keep = keep & np.isfinite(arr) & (arr >= 0)
    return float(arr[keep].sum()) if keep.any() else 0.0


def check_worldpop_resolution_m(pixel_m: float) -> None:
    if not (PIXEL_M_BAND[0] <= pixel_m <= PIXEL_M_BAND[1]):
        raise GateError(
            f"WorldPop pixel {pixel_m:.1f} m is outside the {PIXEL_M_BAND} m band"
        )


def check_worldpop_sum(people: float) -> None:
    if not (POP_SUM_BAND[0] <= people <= POP_SUM_BAND[1]):
        raise GateError(
            f"Sikkim population {people:.0f} is outside {POP_SUM_BAND}"
        )


def check_building_row(confidence: float, area_m2: float, geom_type: str) -> None:
    if geom_type not in ("Polygon", "MultiPolygon", "Point"):
        raise GateError(f"building geometry {geom_type} is not allowed")
    if not (BUILDING_CONFIDENCE[0] <= confidence <= BUILDING_CONFIDENCE[1]):
        raise GateError(f"building confidence {confidence} is outside {BUILDING_CONFIDENCE}")
    if not (BUILDING_AREA_M2[0] < area_m2 < BUILDING_AREA_M2[1]):
        raise GateError(f"building area {area_m2} is outside {BUILDING_AREA_M2}")


def check_building_count(n: int) -> None:
    if not (BUILDING_COUNT_BAND[0] <= n <= BUILDING_COUNT_BAND[1]):
        raise GateError(f"building count {n} is outside {BUILDING_COUNT_BAND}")


def check_forecast_point(lat: float, lon: float, precip_mm: float,
                         soil: float | None, sikkim_bbox: tuple) -> None:
    west, south, east, north = sikkim_bbox
    if not (south - 0.1 <= lat <= north + 0.1 and west - 0.1 <= lon <= east + 0.1):
        raise GateError(f"forecast point {lat},{lon} is outside Sikkim ±0.1°")
    if not np.isfinite(precip_mm) or precip_mm < 0 or precip_mm > 500:
        raise GateError(f"precipitation {precip_mm} mm is not in [0, 500]")
    if soil is not None and (not np.isfinite(soil) or soil < 0 or soil > 0.7):
        raise GateError(f"soil moisture {soil} is not in [0, 0.7]")


def overlap_regression_pp(old_share: float, new_share: float) -> float:
    """Percentage-point change. Positive = more inventory captured."""
    return 100.0 * (new_share - old_share)


def alert_level(is_risk_hotspot: bool, precip_3h_mm: float, precip_24h_mm: float,
                soil: float | None) -> str | None:
    """Watch / warning / critical, or None. Only on corrected risk polygons."""
    if not is_risk_hotspot:
        return None
    if not np.isfinite(precip_3h_mm) or not np.isfinite(precip_24h_mm):
        raise GateError("precipitation is not finite")
    if precip_3h_mm < 0 or precip_24h_mm < 0:
        raise GateError("precipitation must be ≥ 0")
    if precip_24h_mm > 800:
        raise GateError("daily precipitation exceeds 800 mm")
    wet = (
        precip_3h_mm >= 10
        or precip_24h_mm >= 40
        or (soil is not None and np.isfinite(soil) and soil >= 0.40)
    )
    if not wet:
        return None
    if precip_3h_mm >= 25 or precip_24h_mm >= 80:
        return "critical"
    if precip_3h_mm >= 10 or precip_24h_mm >= 40:
        return "warning"
    return "watch"


def assert_overlap_gate(old_share: float, new_share: float) -> float:
    delta = overlap_regression_pp(old_share, new_share)
    if delta < -OVERLAP_REGRESSION_PP:
        raise GateError(
            f"inventory overlap fell {abs(delta):.2f} pp "
            f"(limit {OVERLAP_REGRESSION_PP} pp); manual review required"
        )
    return delta
