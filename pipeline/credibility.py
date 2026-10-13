"""Checks the published Sikkim layers against the analysis grid and known totals.

The script in scripts/run_credibility_audit.py reads the real files. These
functions stay free of file paths so the gates can be tested on tiny inputs.
"""
from __future__ import annotations

import numpy as np

from pipeline.risk_lib import POP_SUM_BAND, GateError, check_worldpop_sum

# Census of India 2011, Sikkim. Registrar General & Census Commissioner.
CENSUS_2011_SIKKIM = 610_577

PROPOSALS = (
    {
        "title": "Keep the forecast on its native 0.1° grid",
        "detail": (
            "Open-Meteo is a 9 km model sampled every 0.1°. Painting it onto "
            "the 30 m analysis grid makes a regional forecast look like a "
            "village measurement. Store the coarse grid and let the tile "
            "reader resample it."
        ),
    },
    {
        "title": "Request 72 hours and label a stale feed",
        "detail": (
            "The forecast asks for two days, so a 72-hour check has no array "
            "to read. Alerts already keep the last file when the API refuses; "
            "the radar clock should say the frame is saved rather than look live."
        ),
    },
)


def legend_cap(p99: float) -> int:
    """Top of the population bar: the 99th percentile, rounded up."""
    if not np.isfinite(p99) or p99 < 0:
        raise GateError(f"population p99 {p99} is not usable")
    return max(1, int(np.ceil(p99 - 1e-9)))


def population_check(people: float) -> dict:
    check_worldpop_sum(people)
    return {
        "ok": True,
        "people": round(float(people), 1),
        "band": list(POP_SUM_BAND),
        "census_2011": CENSUS_2011_SIKKIM,
        "ratio_to_census_2011": round(float(people) / CENSUS_2011_SIKKIM, 3),
    }


def grid_issues(profile: dict, grid: dict) -> list[str]:
    """A layer on the analysis grid shares CRS, size, and the 30 m origin."""
    issues = []
    if str(profile.get("crs")) != str(grid.get("crs")):
        issues.append(f"crs {profile.get('crs')} is not {grid.get('crs')}")
    if int(profile.get("width", -1)) != int(grid["width"]) or int(profile.get("height", -1)) != int(grid["height"]):
        issues.append(
            f"size {profile.get('width')}×{profile.get('height')} is not {grid['width']}×{grid['height']}"
        )
    got = [float(v) for v in profile.get("transform", [])]
    exp = [float(v) for v in grid["transform"]]
    if len(got) != 6 or len(exp) != 6:
        issues.append("transform is missing")
    elif abs(got[2] - exp[2]) > 1 or abs(got[5] - exp[5]) > 1 or abs(got[0] - exp[0]) > 0.01:
        issues.append(f"origin {got[2]:.1f},{got[5]:.1f} is not {exp[2]:.1f},{exp[5]:.1f}")
    return issues


def point_outside(lon: float, lat: float, bbox: tuple[float, float, float, float], pad: float = 0.02) -> bool:
    west, south, east, north = bbox
    return not (west - pad <= lon <= east + pad and south - pad <= lat <= north + pad)


def forecast_anomalies(hourly_mm, hours: int) -> dict:
    """Count gaps. A null hour is a dropout, never a dry hour."""
    values = list(hourly_mm)
    nulls = sum(v is None or (isinstance(v, float) and not np.isfinite(v)) for v in values)
    numbers = [float(v) for v in values if v is not None and np.isfinite(v)]
    zeros = sum(v == 0.0 for v in numbers)
    negatives = sum(v < 0 for v in numbers)
    covers = int(hours) >= 72 and nulls == 0 and len(values) >= 72
    note = (
        "72 h is covered"
        if covers
        else f"horizon is {int(hours)} h with {len(values)} samples; 72 h is not covered"
    )
    return {
        "samples": len(values),
        "nulls": nulls,
        "zeros": zeros,
        "negatives": negatives,
        "nulls_treated_as_zero": False,
        "covers_72h": covers,
        "horizon_note": note,
    }
