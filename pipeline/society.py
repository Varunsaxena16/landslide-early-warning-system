"""Society-facing uses of the corrected risk map.

Before: the published hotspot (people + buildings).
During: rain/soil alerts plus a highway advisory.
After / community: a precursor report and the nearest safe assembly point.

Highways and assembly points come from OpenStreetMap (see
`tools/fetch_society.py`), clipped to the Sikkim outline. Each highway way
is cut into 1 km sections and measured against the published risk polygons
and the GSI inventory points. A school, ground, stadium or hall is a safe
assembly point only when it sits more than 100 m from both.
"""
from __future__ import annotations

import csv
import json
import math
import os
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

from pipeline.alerts import SIKKIM_BBOX
from pipeline.common import OUT_DIR, ROOT, VEC_DIR
from pipeline.risk_lib import GateError

RISK_DIR = OUT_DIR / "risk"
REPORTS_PATH = Path(os.environ.get(
    "REPORTS_PATH", str(RISK_DIR / "community_reports.json")))
PLACES_CSV = Path(os.environ.get(
    "PLACES_FILE", str(ROOT / "reference" / "places_sikkim.csv")))
SOCIETY_DIR = OUT_DIR / "society"
HIGHWAYS_PATH = SOCIETY_DIR / "highways.geojson"
ASSEMBLY_RAW_PATH = SOCIETY_DIR / "assembly_raw.geojson"
HOTSPOTS_PATH = VEC_DIR / "hotspots.geojson"
INVENTORY_PATH = VEC_DIR / "inventory.geojson"
# Saved advisory, so a cold host returns the last build instead of scoring
# every highway section and assembly point on the request that draws them.
CORRIDOR_DISK = RISK_DIR / "corridor.geojson"
SHELTER_DISK = RISK_DIR / "shelters.geojson"
_REFRESH_LOCKS = {
    "corridor": threading.Lock(),
    "shelters": threading.Lock(),
}
SECTION_M = 1000.0
NEAR_M = 250.0
AVOID_M = 100.0
SHELTER_CLEAR_M = 100.0
REPORT_HOURS = 48
SIGN_TYPES = ("crack", "tilt", "spring", "rumble", "blocked_road", "other")
LEVEL_UP = {"watch": "warning", "warning": "critical", "critical": "critical"}

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32645", always_xy=True)
_TO_WGS = Transformer.from_crs("EPSG:32645", "EPSG:4326", always_xy=True)


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlamb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlamb / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlamb = math.radians(lon2 - lon1)
    y = math.sin(dlamb) * math.cos(p2)
    x = (math.cos(p1) * math.sin(p2)
         - math.sin(p1) * math.cos(p2) * math.cos(dlamb))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass(bearing: float) -> str:
    labels = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return labels[int((bearing + 22.5) // 45) % 8]


def _to_utm(geom):
    return shp_transform(lambda x, y, z=None: _TO_UTM.transform(x, y), geom)


def _to_wgs(geom):
    return shp_transform(lambda x, y, z=None: _TO_WGS.transform(x, y), geom)


def load_places(path: Path | None = None) -> list[dict]:
    src = path or PLACES_CSV
    if not src.exists():
        raise GateError(f"missing places file {src}")
    rows = []
    with src.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append({
                "name": row["name"],
                "type": row.get("type") or "",
                "district": row.get("district") or "",
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "note": row.get("note") or "",
            })
    return rows


def _place_index(places: list[dict]) -> dict[str, dict]:
    return {p["name"]: p for p in places}


def densify_line(coords: list[tuple[float, float]], step_m: float = SECTION_M
                 ) -> list[list[tuple[float, float]]]:
    """Split a lon/lat chain into ~step_m sections in UTM 32645."""
    if len(coords) < 2:
        raise GateError("a corridor needs at least two waypoints")
    if step_m <= 0:
        raise GateError("section length must be > 0")
    line = _to_utm(LineString(coords))
    length = line.length
    if length <= 0:
        raise GateError("corridor has no length")
    sections = []
    t = 0.0
    while t < length:
        end = min(t + step_m, length)
        part = _to_wgs(LineString([line.interpolate(t), line.interpolate(end)]))
        sections.append(list(part.coords))
        if end >= length:
            break
        t = end
    return sections


def _read_fc(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("features") or []


def _line_parts(geom: dict) -> list[list[tuple[float, float]]]:
    g = shape(geom)
    if g.geom_type == "LineString":
        return [list(g.coords)]
    if g.geom_type == "MultiLineString":
        return [list(part.coords) for part in g.geoms if len(part.coords) >= 2]
    return []


def section_highways(features: list[dict]) -> list[dict]:
    """Cut each OSM way into ~1 km pieces, keeping the road's own name and ref."""
    out = []
    sid = 0
    for f in features:
        props = f.get("properties") or {}
        for coords in _line_parts(f["geometry"]):
            try:
                parts = densify_line([(c[0], c[1]) for c in coords])
            except GateError:
                continue
            for pair in parts:
                mid = LineString(pair).interpolate(0.5, normalized=True)
                sid += 1
                label = props.get("ref") or props.get("name") or props.get("highway") or "road"
                out.append({
                    "id": sid,
                    "name": props.get("name") or "",
                    "ref": props.get("ref") or "",
                    "highway": props.get("highway") or "",
                    "label": label,
                    "osm_id": props.get("osm_id"),
                    "lon": round(mid.x, 5),
                    "lat": round(mid.y, 5),
                    "geometry": {"type": "LineString", "coordinates":
                                 [[round(x, 5), round(y, 5)] for x, y in pair]},
                })
    return out


def score_section(dist_m: float, precip_24h_mm: float, precip_72h_mm: float = 0.0,
                  slope_deg: float | None = None,
                  dist_slide_m: float | None = None) -> str:
    """Avoid / caution / open.

    Avoid needs the road within 100 m of a risk hotspot or a mapped landslide,
    and forecast rain across the line. Caution is proximity to either, or rain,
    without both.
    """
    slide = float("inf") if dist_slide_m is None else float(dist_slide_m)
    if dist_m < 0 or slide < 0:
        raise GateError("distance cannot be negative")
    wet_avoid = precip_24h_mm >= 40 or precip_72h_mm >= 100
    if (dist_m <= AVOID_M or slide <= AVOID_M) and wet_avoid:
        return "avoid"
    if (dist_m <= NEAR_M or slide <= NEAR_M
            or precip_24h_mm >= 20
            or (slope_deg is not None and slope_deg >= 30)):
        return "caution"
    return "open"


def section_action(status: str, dist_hotspot_m: float, dist_slide_m: float,
                   precip_24h_mm: float) -> str:
    near_risk = dist_hotspot_m <= NEAR_M
    near_slide = dist_slide_m <= NEAR_M
    wet = precip_24h_mm >= 20
    if status == "avoid":
        bits = []
        if dist_hotspot_m <= AVOID_M:
            bits.append("a risk hotspot")
        if dist_slide_m <= AVOID_M:
            bits.append("a mapped landslide")
        return ("Do not travel — within 100 m of "
                + " and ".join(bits or ["mapped danger"])
                + ", and forecast rain has crossed the line.")
    if status == "caution":
        bits = []
        if near_risk:
            bits.append("risk hotspot within 250 m")
        if near_slide:
            bits.append("past landslide within 250 m")
        if wet:
            bits.append("forecast rain")
        return "Limit travel — " + ", ".join(bits or ["steep ground"]) + "."
    return "Open. No mapped landslide or risk hotspot within 250 m."


def _clean_utm(geom):
    g = _to_utm(shape(geom))
    return g if g.is_valid else g.buffer(0)


def _hazard_index(hotspots: list[dict]):
    geoms, ranks = [], []
    for f in hotspots:
        geoms.append(_clean_utm(f["geometry"]))
        ranks.append(f["properties"].get("rank_area"))
    tree = STRtree(geoms) if geoms else None
    return geoms, ranks, tree


def nearest_hazard(lon: float, lat: float, index) -> tuple[float, int | None]:
    geoms, ranks, tree = index
    if not geoms or tree is None:
        return float("inf"), None
    pt = Point(*_TO_UTM.transform(lon, lat))
    i = int(tree.nearest(pt))
    return float(geoms[i].distance(pt)), ranks[i]


def nearest_feature(geom: dict, index) -> tuple[float, object]:
    """Minimum distance in metres from a WGS84 geometry to the indexed shapes."""
    geoms, labels, tree = index
    if not geoms or tree is None:
        return float("inf"), None
    g = _to_utm(shape(geom))
    i = int(tree.nearest(g))
    return float(geoms[i].distance(g)), labels[i]


def _slide_index(slides: list[dict]):
    geoms, names = [], []
    for f in slides:
        geoms.append(_clean_utm(f["geometry"]))
        p = f.get("properties") or {}
        names.append(p.get("slide_name") or p.get("slide_no") or "mapped landslide")
    tree = STRtree(geoms) if geoms else None
    return geoms, names, tree


def load_slides() -> list[dict]:
    return _read_fc(INVENTORY_PATH)


def rain_near(lon: float, lat: float, alerts: list[dict],
              radius_m: float = 2000.0) -> tuple[float, float]:
    p24 = p72 = 0.0
    for f in alerts:
        p = f["properties"]
        alon = p.get("lon")
        alat = p.get("lat")
        if alon is None or alat is None:
            continue
        if haversine_m(lon, lat, float(alon), float(alat)) > radius_m:
            continue
        p24 = max(p24, float(p.get("precip_24h_mm") or 0))
        p72 = max(p72, float(p.get("precip_72h_mm") or 0))
    return p24, p72


def load_hotspots() -> list[dict]:
    if not HOTSPOTS_PATH.exists():
        return []
    return json.loads(HOTSPOTS_PATH.read_text())["features"]


def _finite(dist: float):
    return None if dist == float("inf") else round(dist, 1)


_CORRIDOR_CACHE: dict = {}


def _mtime(path: Path) -> float:
    return round(path.stat().st_mtime, 3) if path.exists() else 0.0


def _read_disk(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_disk(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")))
    tmp.replace(path)


def _schedule_refresh(name: str, build) -> None:
    """Rebuild off the request so the map can draw the last saved file now."""
    lock = _REFRESH_LOCKS[name]
    if not lock.acquire(blocking=False):
        return

    def run():
        try:
            build()
        finally:
            lock.release()

    threading.Thread(target=run, name=f"society-{name}", daemon=True).start()


def _corridor_stamp():
    from pipeline.alerts import load_alerts
    alerts = apply_report_bumps(load_alerts()).get("features") or []
    rain = tuple(sorted(
        (int((f.get("properties") or {}).get("rank_area") or 0),
         round(float((f.get("properties") or {}).get("precip_24h_mm") or 0), 1))
        for f in alerts
    ))
    return (
        _mtime(HIGHWAYS_PATH),
        _mtime(HOTSPOTS_PATH),
        _mtime(INVENTORY_PATH),
        rain,
    )


def _stamp_token(stamp) -> str:
    hw, hs, inv, rain = stamp
    rain_s = ",".join(f"{int(rank)}:{float(mm):.1f}" for rank, mm in rain)
    return f"{hw:.3f}|{hs:.3f}|{inv:.3f}|{rain_s}"


def _store_corridor(stamp) -> dict:
    from pipeline.alerts import load_alerts
    alerts = apply_report_bumps(load_alerts()).get("features") or []
    fc = build_corridor(hotspots=load_hotspots(), alerts=alerts)
    fc.setdefault("meta", {})["built_stamp"] = _stamp_token(stamp)
    try:
        _write_disk(CORRIDOR_DISK, fc)
    except OSError:
        pass
    _CORRIDOR_CACHE["stamp"] = stamp
    _CORRIDOR_CACHE["fc"] = fc
    return fc


def corridor_geojson() -> dict:
    """The highway advisory.

    A cold process returns the last saved file immediately. Scoring every
    section is deferred until the roads, the hazard polygons, or the rain
    have changed, and that refresh does not hold the request.
    """
    stamp = _corridor_stamp()
    if _CORRIDOR_CACHE.get("stamp") == stamp and _CORRIDOR_CACHE.get("fc"):
        return _CORRIDOR_CACHE["fc"]
    disk = _read_disk(CORRIDOR_DISK)
    if disk and disk.get("meta", {}).get("built_stamp") == _stamp_token(stamp):
        _CORRIDOR_CACHE["stamp"] = stamp
        _CORRIDOR_CACHE["fc"] = disk
        return disk
    if disk and disk.get("features"):
        _schedule_refresh("corridor", lambda: _store_corridor(stamp))
        return disk
    return _store_corridor(stamp)


def build_corridor(hotspots: list[dict] | None = None,
                   alerts: list[dict] | None = None,
                   highways: list[dict] | None = None,
                   slides: list[dict] | None = None,
                   slopes: dict[int, float] | None = None) -> dict:
    """1 km sections of the OSM highway network, scored against risk and GSI."""
    hw = highways if highways is not None else _read_fc(HIGHWAYS_PATH)
    sections = section_highways(hw)
    h_index = _hazard_index(hotspots if hotspots is not None else load_hotspots())
    s_index = _slide_index(slides if slides is not None else load_slides())
    alert_feats = alerts if alerts is not None else []
    features = []
    counts = {"avoid": 0, "caution": 0, "open": 0}
    for s in sections:
        dist, rank = nearest_feature(s["geometry"], h_index)
        sdist, sname = nearest_feature(s["geometry"], s_index)
        p24, p72 = rain_near(s["lon"], s["lat"], alert_feats)
        slope = None if not slopes else slopes.get(s["id"])
        status = score_section(dist, p24, p72, slope,
                                None if sdist == float("inf") else sdist)
        counts[status] += 1
        features.append({
            "type": "Feature",
            "id": s["id"],
            "properties": {
                "id": s["id"],
                "name": s["name"],
                "ref": s["ref"],
                "highway": s["highway"],
                "label": s["label"],
                "osm_id": s["osm_id"],
                "lon": s["lon"],
                "lat": s["lat"],
                "status": status,
                "action": section_action(status, dist, sdist, p24),
                "dist_hotspot_m": _finite(dist),
                "nearest_rank": rank,
                "dist_slide_m": _finite(sdist),
                "nearest_slide": sname,
                "precip_24h_mm": round(p24, 2),
                "precip_72h_mm": round(p72, 2),
                "slope_deg": None if slope is None else round(float(slope), 1),
            },
            "geometry": s["geometry"],
        })
    named = sorted({
        (f["properties"]["ref"] or f["properties"]["name"])
        for f in features if f["properties"]["ref"] or f["properties"]["name"]
    })
    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "sections": len(features),
            "counts": counts,
            "named_roads": named[:40],
            "source": "OpenStreetMap highways, clipped to Sikkim, 1 km sections",
            "scored_against": ["published risk polygons", "GSI landslide inventory"],
        },
    }


def _as_point(f: dict) -> tuple[dict, float, float]:
    if f.get("type") == "Feature":
        lon, lat = f["geometry"]["coordinates"]
        return f.get("properties") or {}, float(lon), float(lat)
    return f, float(f["lon"]), float(f["lat"])


def candidate_shelters(points: list[dict] | None = None,
                       hotspots: list[dict] | None = None,
                       slides: list[dict] | None = None) -> tuple[list[dict], int]:
    """OSM schools, grounds and halls clear of both hotspots and mapped slides.

    Returns (safe points, number dropped for sitting on a hazard).
    """
    raw = points if points is not None else _read_fc(ASSEMBLY_RAW_PATH)
    h_index = _hazard_index(hotspots if hotspots is not None else load_hotspots())
    s_index = _slide_index(slides if slides is not None else load_slides())
    safe, dropped = [], 0
    for f in raw:
        props, lon, lat = _as_point(f)
        kind = props.get("kind") or ""
        if not kind:
            continue
        hd, rank = nearest_hazard(lon, lat, h_index)
        sd, sname = nearest_feature(
            {"type": "Point", "coordinates": [lon, lat]}, s_index)
        if hd <= SHELTER_CLEAR_M or sd <= SHELTER_CLEAR_M:
            dropped += 1
            continue
        safe.append({
            "name": props.get("name") or f"Unnamed {kind}",
            "kind": kind,
            "osm_id": props.get("osm_id"),
            "lon": lon,
            "lat": lat,
            "dist_hotspot_m": _finite(hd),
            "nearest_rank": rank,
            "dist_slide_m": _finite(sd),
            "nearest_slide": sname,
        })
    return safe, dropped


_SAFE_CACHE: dict = {}


def _shelter_stamp() -> tuple[float, float, float]:
    return (
        _mtime(ASSEMBLY_RAW_PATH),
        _mtime(HOTSPOTS_PATH),
        _mtime(INVENTORY_PATH),
    )


def _shelter_token(stamps) -> str:
    return "|".join(f"{m:.3f}" for m in stamps)


def _safe_from_fc(fc: dict) -> tuple[list[dict], int]:
    safe = [dict(f.get("properties") or {}) for f in fc.get("features") or []]
    dropped = int((fc.get("meta") or {}).get("dropped_on_hazard") or 0)
    return safe, dropped


def _shelters_fc(safe: list[dict], dropped: int) -> dict:
    features = [{
        "type": "Feature",
        "properties": s,
        "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
    } for s in safe]
    kinds: dict[str, int] = {}
    for s in safe:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "count": len(features),
            "dropped_on_hazard": dropped,
            "by_kind": kinds,
            "source": "OpenStreetMap schools, colleges, grounds, stadiums, halls",
            "rule": "kept only if more than 100 m from every risk hotspot and every GSI point",
        },
    }


def _store_shelters(stamps) -> tuple[list[dict], int]:
    safe, dropped = candidate_shelters()
    fc = _shelters_fc(safe, dropped)
    fc["meta"]["built_stamp"] = _shelter_token(stamps)
    try:
        _write_disk(SHELTER_DISK, fc)
    except OSError:
        pass
    _SAFE_CACHE["stamps"] = stamps
    _SAFE_CACHE["safe"] = safe
    _SAFE_CACHE["dropped"] = dropped
    return safe, dropped


def _cached_safe() -> tuple[list[dict], int]:
    """Safe assembly points are static. A saved file is served at once."""
    stamps = _shelter_stamp()
    if _SAFE_CACHE.get("stamps") == stamps and _SAFE_CACHE.get("safe") is not None:
        return _SAFE_CACHE["safe"], _SAFE_CACHE["dropped"]
    disk = _read_disk(SHELTER_DISK)
    if disk and (disk.get("meta") or {}).get("built_stamp") == _shelter_token(stamps):
        safe, dropped = _safe_from_fc(disk)
        _SAFE_CACHE["stamps"] = stamps
        _SAFE_CACHE["safe"] = safe
        _SAFE_CACHE["dropped"] = dropped
        return safe, dropped
    if disk and disk.get("features"):
        _schedule_refresh("shelters", lambda: _store_shelters(stamps))
        return _safe_from_fc(disk)
    return _store_shelters(stamps)


def shelters_geojson(points: list[dict] | None = None,
                     hotspots: list[dict] | None = None,
                     slides: list[dict] | None = None) -> dict:
    if points is None and hotspots is None and slides is None:
        safe, dropped = _cached_safe()
    else:
        safe, dropped = candidate_shelters(points, hotspots, slides)
    return _shelters_fc(safe, dropped)


def nearest_shelters(lon: float, lat: float, limit: int = 3,
                     points: list[dict] | None = None,
                     hotspots: list[dict] | None = None,
                     slides: list[dict] | None = None) -> dict:
    if not (SIKKIM_BBOX[0] <= lon <= SIKKIM_BBOX[2]
            and SIKKIM_BBOX[1] <= lat <= SIKKIM_BBOX[3]):
        raise GateError("point is outside Sikkim")
    if points is None and hotspots is None and slides is None:
        safe, dropped = _cached_safe()
    else:
        safe, dropped = candidate_shelters(points, hotspots, slides)
    rows = []
    for s in safe:
        d = haversine_m(lon, lat, s["lon"], s["lat"])
        brg = bearing_deg(lon, lat, s["lon"], s["lat"])
        rows.append({
            **s,
            "distance_m": round(d, 1),
            "bearing_deg": round(brg, 1),
            "bearing": compass(brg),
            "walk_min": max(1, int(round(d / 1000.0 / 4.5 * 60))),
        })
    rows.sort(key=lambda r: r["distance_m"])
    return {
        "lon": lon, "lat": lat,
        "shelters": rows[:limit],
        "candidates": len(rows),
        "dropped_on_hazard": dropped,
    }


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_reports(path: Path | None = None) -> list[dict]:
    src = path or REPORTS_PATH
    if not src.exists():
        return []
    try:
        doc = json.loads(src.read_text())
    except (OSError, ValueError):
        return []
    return list(doc.get("reports") or [])


def save_reports(reports: list[dict], path: Path | None = None) -> None:
    dest = path or REPORTS_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"reports": reports}, indent=2))


def add_report(sign: str, lon: float, lat: float, note: str = "",
               now: datetime | None = None,
               hotspots: list[dict] | None = None,
               path: Path | None = None) -> dict:
    if sign not in SIGN_TYPES:
        raise GateError(f"unknown sign '{sign}'")
    if not (SIKKIM_BBOX[0] <= lon <= SIKKIM_BBOX[2]
            and SIKKIM_BBOX[1] <= lat <= SIKKIM_BBOX[3]):
        raise GateError("report is outside Sikkim")
    now = now or datetime.now(timezone.utc)
    dist, rank = nearest_hazard(lon, lat, _hazard_index(
        hotspots if hotspots is not None else load_hotspots()))
    inside = dist <= NEAR_M
    rec = {
        "id": f"r-{int(now.timestamp())}-{rank or 0}",
        "sign": sign,
        "note": (note or "")[:280],
        "lon": round(float(lon), 5),
        "lat": round(float(lat), 5),
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "rank_area": rank if inside else None,
        "inside_hotspot": inside,
        "verified": False,
    }
    reports = load_reports(path)
    reports.append(rec)
    save_reports(reports, path)
    return rec


def apply_report_bumps(alerts_fc: dict, reports: list[dict] | None = None,
                       now: datetime | None = None) -> dict:
    """A fresh unverified report on a hotspot raises that alert one level."""
    reports = reports if reports is not None else load_reports()
    now = now or datetime.now(timezone.utc)
    out = deepcopy(alerts_fc)
    cutoff = now - timedelta(hours=REPORT_HOURS)
    for feat in out.get("features") or []:
        rank = feat["properties"].get("rank_area")
        hits = [
            r for r in reports
            if r.get("rank_area") == rank
            and r.get("inside_hotspot")
            and _parse_ts(r["created_at"]) >= cutoff
        ]
        if not hits:
            continue
        old = feat["properties"].get("level")
        feat["properties"]["level"] = LEVEL_UP.get(old, old)
        feat["properties"]["community_raised"] = True
        feat["properties"]["community_reports"] = len(hits)
    return out
