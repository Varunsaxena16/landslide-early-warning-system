"""Early warning on corrected risk hotspots.

Rain and soil are a time trigger. They do not change the static risk polygons.
Open-Meteo is the only live feed (hourly precipitation + soil_moisture_0_to_7cm).
NASA SMAP is not fetched.

Batches of 40 points, 30-minute on-disk cache, retry then fail loud on HTTP 429.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from pipeline.common import OUT_DIR, VEC_DIR
from pipeline.risk_lib import GateError, alert_level, check_forecast_point

KOLKATA = ZoneInfo("Asia/Kolkata")
SIKKIM_BBOX = (88.010695, 27.073148, 88.938677, 28.134655)
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
BATCH = 40
CACHE_MINUTES = 30
RISK_DIR = OUT_DIR / "risk"
CACHE_PATH = RISK_DIR / "forecast_cache.json"
ALERTS_PATH = RISK_DIR / "active_alerts.geojson"

GetFn = Callable[[str], tuple[int, object]]


def batches(items: list, size: int = BATCH):
    if size < 1:
        raise GateError("batch size must be ≥ 1")
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _parse_local(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=KOLKATA)


def summarise_hourly(hourly: dict, now: datetime) -> dict:
    """3 h and 24 h forecast rain from `now`, plus soil at the current hour."""
    times = [_parse_local(t) for t in hourly.get("time") or []]
    raw_p = list(hourly.get("precipitation") or [])
    nulls = sum(p is None for p in raw_p)
    # A missing hour is logged, then treated as dry so the sum stays defined.
    precip = [0.0 if p is None else float(p) for p in raw_p]
    soil = hourly.get("soil_moisture_0_to_7cm")
    if len(times) != len(precip) or not times:
        raise GateError("Open-Meteo hourly block is missing or ragged")
    if any(p < 0 or p > 500 for p in precip):
        raise GateError("hourly precipitation is outside [0, 500] mm")
    if now.tzinfo is None:
        now = now.replace(tzinfo=KOLKATA)
    now = now.astimezone(KOLKATA).replace(minute=0, second=0, microsecond=0)
    try:
        idx = next(i for i, t in enumerate(times) if t >= now)
    except StopIteration:
        raise GateError("forecast hours do not cover the current time")
    p3 = float(sum(precip[idx:idx + 3]))
    p24 = float(sum(precip[idx:idx + 24]))
    window72 = precip[idx:idx + 72]
    if p24 > 800:
        raise GateError("daily precipitation exceeds 800 mm")
    sm = None
    if soil is not None:
        if len(soil) != len(times):
            raise GateError("soil moisture series does not match the hours")
        raw = soil[idx]
        sm = None if raw is None else float(raw)
        if sm is not None and (sm < 0 or sm > 0.7):
            raise GateError(f"soil moisture {sm} is not in [0, 0.7]")
    return {
        "precip_3h_mm": round(p3, 2),
        "precip_24h_mm": round(p24, 2),
        "precip_72h_mm": round(float(sum(window72)), 2) if len(window72) >= 72 else None,
        "precip_72h_hours_present": len(window72),
        "precip_null_hours": nulls,
        "precip_null_hours_24h": sum(p is None for p in raw_p[idx:idx + 24]),
        "soil_moisture_0_to_7cm": sm,
        "valid_from": times[idx].isoformat(),
    }


def default_get(url: str) -> tuple[int, object]:
    req = Request(url, headers={"User-Agent": "PahiroWatch-alerts/1.0"})
    try:
        with urlopen(req, timeout=40) as resp:
            return resp.status, json.load(resp)
    except HTTPError as e:
        return e.code, None


def request_json(url: str, get: GetFn | None = None,
                 sleeps: tuple = (0.0, 2.0, 4.0)) -> object:
    get = get or default_get
    last = None
    for wait in sleeps:
        if wait:
            time.sleep(wait)
        try:
            status, body = get(url)
        except URLError as e:
            raise GateError(f"Open-Meteo unreachable: {e}") from e
        if status == 200 and body is not None:
            return body
        last = status
        if status != 429:
            raise GateError(f"Open-Meteo HTTP {status}")
    raise GateError("Open-Meteo rate-limited (HTTP 429)")


def forecast_url(lats: Iterable[float], lons: Iterable[float]) -> str:
    qs = urlencode({
        "latitude": ",".join(f"{v:.5f}" for v in lats),
        "longitude": ",".join(f"{v:.5f}" for v in lons),
        "hourly": "precipitation,soil_moisture_0_to_7cm",
        "forecast_days": 2,
        "timezone": "Asia/Kolkata",
    })
    return f"{OPEN_METEO}?{qs}"


def _as_list(payload):
    if isinstance(payload, list):
        return payload
    return [payload]


def fetch_forecasts(points: list[dict], get: GetFn | None = None,
                    now: datetime | None = None, sleeps: tuple = (0.0, 2.0, 4.0)) -> dict:
    """points: {rank_area, lon, lat}. Returns rank_area -> summary."""
    now = now or datetime.now(KOLKATA)
    out = {}
    for batch in batches(points, BATCH):
        url = forecast_url((p["lat"] for p in batch), (p["lon"] for p in batch))
        payload = _as_list(request_json(url, get=get, sleeps=sleeps))
        if len(payload) != len(batch):
            raise GateError(
                f"Open-Meteo returned {len(payload)} series for {len(batch)} points"
            )
        for spec, block in zip(batch, payload):
            hourly = block.get("hourly") or {}
            summary = summarise_hourly(hourly, now)
            hourly_max = max((float(x) for x in (hourly.get("precipitation") or [0])), default=0.0)
            check_forecast_point(
                spec["lat"], spec["lon"], hourly_max,
                summary["soil_moisture_0_to_7cm"], SIKKIM_BBOX)
            out[spec["rank_area"]] = summary
    if len(out) != len(points):
        raise GateError("a forecast point was dropped")
    return out


def cache_is_fresh(path: Path = CACHE_PATH, now: datetime | None = None) -> bool:
    if not path.exists():
        return False
    try:
        doc = json.loads(path.read_text())
        fetched = datetime.fromisoformat(doc["fetched_at"])
    except (OSError, KeyError, ValueError):
        return False
    now = now or datetime.now(timezone.utc)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return now - fetched <= timedelta(minutes=CACHE_MINUTES)


def build_alerts(hotspot_points: list[dict], forecasts: dict) -> dict:
    features = []
    for f in hotspot_points:
        p = f["properties"]
        rank = p["rank_area"]
        fc = forecasts.get(rank)
        if fc is None:
            raise GateError(f"no forecast for hotspot {rank}")
        level = alert_level(
            True, fc["precip_3h_mm"], fc["precip_24h_mm"],
            fc["soil_moisture_0_to_7cm"])
        if not level:
            continue
        props = {
            **{k: p.get(k) for k in (
                "rank_area", "rank_risk", "lon", "lat", "area_km2",
                "population", "building_count", "Population_Exposed",
                "Buildings_At_Risk", "risk_score", "mean_index")},
            **fc,
            "level": level,
            "action": ACTION[level],
        }
        features.append({
            "type": "Feature",
            "id": rank,
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "source": "Open-Meteo",
            "timezone": "Asia/Kolkata",
            "hotspots_scored": len(hotspot_points),
            "active": len(features),
        },
    }


ACTION = {
    "watch": "Wet soil or rising rain on an occupied slope — watch this patch.",
    "warning": "Forecast rain has crossed the warning line — limit travel through this patch.",
    "critical": "Forecast rain is at the critical line — avoid this patch.",
}


def _hotspot_points() -> list[dict]:
    path = VEC_DIR / "hotspot_points.geojson"
    if not path.exists():
        raise GateError(f"missing {path}")
    return json.loads(path.read_text())["features"]


def refresh_alerts(get: GetFn | None = None, now: datetime | None = None,
                   force: bool = False) -> dict:
    RISK_DIR.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    if not force and cache_is_fresh(now=now):
        return json.loads(ALERTS_PATH.read_text()) if ALERTS_PATH.exists() else {
            "type": "FeatureCollection", "features": []}
    pts = [{"rank_area": f["properties"]["rank_area"],
            "lon": f["properties"]["lon"], "lat": f["properties"]["lat"]}
           for f in _hotspot_points()]
    forecasts = fetch_forecasts(pts, get=get, now=now.astimezone(KOLKATA) if now.tzinfo else now)
    alerts = build_alerts(_hotspot_points(), forecasts)
    fetched = datetime.now(timezone.utc).isoformat()
    CACHE_PATH.write_text(json.dumps({
        "fetched_at": fetched,
        "points": {str(k): v for k, v in forecasts.items()},
    }))
    alerts["meta"]["fetched_at"] = fetched
    ALERTS_PATH.write_text(json.dumps(alerts))
    return alerts


def load_alerts() -> dict:
    if ALERTS_PATH.exists():
        return json.loads(ALERTS_PATH.read_text())
    return {"type": "FeatureCollection", "features": [],
            "meta": {"active": 0, "hotspots_scored": 0, "source": "Open-Meteo"}}
