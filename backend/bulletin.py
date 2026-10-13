"""Live hazard bulletin for Sikkim and the neighbouring hills.

Three public feeds, no API key. The dashboard does not invent events.

* USGS FDSN — earthquakes of magnitude 4 and above within 420 km of
  Gangtok over the last 180 days. That radius reaches east Nepal,
  western Bhutan and the Tawang hills.
* GDACS — current earthquake, flood and tropical-cyclone alerts whose
  point falls inside the neighbourhood box (Sikkim, Darjeeling,
  Kalimpong, Siliguri, east Nepal, west Bhutan).
* Google News RSS — headlines that name a hill town here and a hazard
  (landslide, flood, heavy rain, earthquake).

A failed refresh keeps the last saved bulletin.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from pipeline.common import OUT_DIR
from pipeline.risk_lib import GateError

# Gangtok, the point the earthquake radius and the distance note are measured from.
GANGTOK = (88.62, 27.33)
QUAKE_RADIUS_KM = 420
QUAKE_DAYS = 180
NEWS_DAYS = 21

# Sikkim plus the hill districts that share its slopes and rivers.
BOX = (86.4, 26.0, 91.6, 28.8)  # west, south, east, north

USGS_URL = (
    "https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
    f"&latitude={GANGTOK[1]}&longitude={GANGTOK[0]}&maxradiuskm={QUAKE_RADIUS_KM}"
    "&minmagnitude=4&orderby=time&limit=8&starttime={start}"
)
GDACS_URL = (
    "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
    "?eventlist=EQ,FL,TC&alertlevel=Green;Orange;Red"
)
NEWS_URL = (
    "https://news.google.com/rss/search?q="
    + quote(
        '(Sikkim OR Darjeeling OR Kalimpong OR Gangtok OR Gyalshing OR Siliguri) '
        '(landslide OR earthquake OR flood OR "heavy rain" OR cloudburst)'
    )
    + "&hl=en-IN&gl=IN&ceid=IN:en"
)

CACHE_PATH = OUT_DIR / "risk" / "bulletin.json"
CACHE_S = 600

PLACES = (
    "gangtok", "darjeeling", "kalimpong", "gyalshing", "rimbi", "mangan",
    "namchi", "pakyong", "siliguri", "rangpo", "soreng", "chungthang", "sikkim",
)
# A shared town or outcome means two headlines are the same incident.
_STORY = (
    "uninhabitable", "rimbi", "gyalshing", "chungthang", "mangan", "dikchu",
    "rangpo", "namchi", "soreng", "kalimpong", "darjeeling", "gangtok", "siliguri",
)
HAZARDS = (
    "landslide", "mudslide", "earthquake", "tremor", "flood", "inundat",
    "rain", "cloudburst", "cyclone",
)
_STOP = {
    "the", "a", "an", "of", "in", "on", "and", "to", "for", "after",
    "amid", "with", "from", "its", "that", "this",
}
_KIND_WORDS = (
    ("landslide", "landslide"), ("mudslide", "landslide"),
    ("earthquake", "earthquake"), ("tremor", "earthquake"),
    ("flood", "flood"), ("inundat", "flood"),
    ("cyclone", "cyclone"), ("rain", "rainfall"), ("cloudburst", "rainfall"),
)
_GDACS_KIND = {"EQ": "earthquake", "FL": "flood", "TC": "cyclone"}
_LEVEL_RANK = {"critical": 0, "watch": 1, "info": 2}

_mem: dict = {"at": 0.0, "doc": None}


def _haversine_km(lon: float, lat: float) -> float:
    lon1, lat1 = math.radians(GANGTOK[0]), math.radians(GANGTOK[1])
    lon2, lat2 = math.radians(lon), math.radians(lat)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _in_box(lon: float, lat: float) -> bool:
    west, south, east, north = BOX
    return west <= lon <= east and south <= lat <= north


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind_from_text(text: str) -> str:
    t = text.lower()
    for needle, kind in _KIND_WORDS:
        if needle in t:
            return kind
    return "news"


def _level(kind: str, title: str, mag: float | None = None, alert: str | None = None) -> str:
    t = title.lower()
    if mag is not None and mag >= 6:
        return "critical"
    if alert == "Red" or any(w in t for w in ("killed", "deaths", "dead", "uninhabitable", "red alert")):
        return "critical"
    if mag is not None and mag >= 4.5:
        return "watch"
    if alert == "Orange" or kind in ("landslide", "flood", "earthquake", "cyclone"):
        return "watch"
    return "info"


def _item(kind: str, title: str, *, when: datetime | None, source: str,
          place: str = "", url: str = "", lon: float | None = None,
          lat: float | None = None, mag: float | None = None,
          alert: str | None = None, ident: str = "") -> dict:
    return {
        "id": ident or f"{source}:{title[:48]}",
        "kind": kind,
        "level": _level(kind, title, mag, alert),
        "title": title,
        "place": place,
        "when": _iso(when),
        "source": source,
        "url": url,
        "lon": lon,
        "lat": lat,
        "mag": mag,
    }


def items_from_usgs(doc: dict, now: datetime | None = None) -> list[dict]:
    """Earthquakes already filtered by USGS to the Gangtok radius."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=QUAKE_DAYS)
    out = []
    for feat in doc.get("features") or []:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2 or props.get("mag") is None:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        mag = float(props["mag"])
        if mag < 4:
            continue
        when = datetime.fromtimestamp(int(props.get("time") or 0) / 1000, tz=timezone.utc)
        if when < cutoff:
            continue
        where = str(props.get("place") or "near Sikkim")
        km = round(_haversine_km(lon, lat))
        out.append(_item(
            "earthquake",
            f"M{mag:.1f} · {where}",
            when=when,
            source="USGS",
            place=f"{km} km from Gangtok",
            url=str(props.get("url") or ""),
            lon=round(lon, 4),
            lat=round(lat, 4),
            mag=mag,
            ident=str(props.get("code") or props.get("id") or where),
        ))
    return out


def items_from_gdacs(doc: dict) -> list[dict]:
    """Keep earthquake, flood and cyclone points inside the neighbourhood box."""
    out = []
    for feat in doc.get("features") or []:
        props = feat.get("properties") or {}
        kind = _GDACS_KIND.get(str(props.get("eventtype") or ""))
        if not kind:
            continue
        current = props.get("iscurrent")
        if current in (False, "false", 0, "0"):
            continue
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        if not _in_box(lon, lat):
            continue
        name = str(props.get("name") or props.get("description") or kind)
        country = str(props.get("country") or "").strip()
        alert = str(props.get("alertlevel") or "")
        when = _parse_loose(props.get("fromdate"))
        url = props.get("url")
        if isinstance(url, dict):
            url = url.get("report") or ""
        km = round(_haversine_km(lon, lat))
        out.append(_item(
            kind,
            name,
            when=when,
            source="GDACS",
            place=" · ".join(p for p in (country, f"{km} km from Gangtok") if p),
            url=str(url or ""),
            lon=round(lon, 4),
            lat=round(lat, 4),
            alert=alert or None,
            ident=f"gdacs-{props.get('eventtype')}-{props.get('eventid')}",
        ))
    return out


def _parse_loose(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _tokens(title: str) -> list[str]:
    head = title.split(" - ")[0].lower()
    return [w for w in re.findall(r"[a-z0-9]+", head) if w not in _STOP and len(w) > 2][:10]


def _same_story(a: list[str], b: list[str], title_a: str = "", title_b: str = "") -> bool:
    sa, sb = set(a), set(b)
    if sa and sb and len(sa & sb) / len(sa | sb) >= 0.5:
        return True
    left, right = title_a.lower(), title_b.lower()
    return any(word in left and word in right for word in _STORY)


def _named_place(title: str) -> str:
    t = title.lower()
    for name in PLACES:
        if name in t:
            return name.title()
    return "Sikkim hills"


def items_from_news(xml_text: str, now: datetime | None = None) -> list[dict]:
    """Headlines that name this neighbourhood and a hazard. Near-duplicates collapse."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=NEWS_DAYS)
    root = ElementTree.fromstring(xml_text)
    kept: list[tuple[list[str], dict]] = []
    for node in root.findall(".//item"):
        raw = (node.findtext("title") or "").strip()
        source = (node.findtext("source") or "").strip()
        title = raw
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        blob = title.lower()
        if not any(p in blob for p in PLACES) or not any(h in blob for h in HAZARDS):
            continue
        when = None
        try:
            when = parsedate_to_datetime(node.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        if when is not None and when < cutoff:
            continue
        kind = _kind_from_text(title)
        item = _item(
            kind, title, when=when, source=source or "News",
            place=_named_place(title),
            url=(node.findtext("link") or "").strip(),
            ident=(node.findtext("guid") or title)[:80],
        )
        tokens = _tokens(title)
        matches = [i for i, (prev, old) in enumerate(kept)
                   if _same_story(tokens, prev, title, old["title"])]
        if not matches:
            kept.append((tokens, item))
            continue
        choice = item
        for i in matches:
            choice = _prefer(choice, kept[i][1])
        for i in reversed(matches):
            kept.pop(i)
        kept.append((_tokens(choice["title"]), choice))
    return [item for _, item in kept][:4]


def _prefer(new: dict, old: dict) -> dict:
    """Keep the sharper of two headlines about one incident."""
    if _LEVEL_RANK.get(new["level"], 9) != _LEVEL_RANK.get(old["level"], 9):
        return new if _LEVEL_RANK[new["level"]] < _LEVEL_RANK[old["level"]] else old
    return new if (new["when"] or "") >= (old["when"] or "") else old


def merge_items(*groups: list[dict]) -> list[dict]:
    """Critical first, then the newest. Cap the strip so it stays readable."""
    items = [item for group in groups for item in group]
    # Stable: newest first, then critical ahead of watch and info.
    items.sort(key=lambda it: it["when"] or "", reverse=True)
    items.sort(key=lambda it: _LEVEL_RANK.get(it["level"], 9))
    return items[:12]


def quiet_item(now: datetime) -> dict:
    return _item(
        "info",
        "No M4+ earthquake, GDACS alert, or disaster headline in the Sikkim neighbourhood in this window.",
        when=now,
        source="USGS · GDACS · Google News",
        place="Sikkim and nearby hills",
        ident="quiet",
    )


def build_bulletin(usgs: dict | None, gdacs: dict | None, news_xml: str | None,
                   now: datetime | None = None, errors: list[str] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    errors = list(errors or [])
    groups = []
    if usgs is not None:
        groups.append(items_from_usgs(usgs, now))
    if gdacs is not None:
        groups.append(items_from_gdacs(gdacs))
    if news_xml is not None:
        groups.append(items_from_news(news_xml, now))
    items = merge_items(*groups) if groups else []
    if not items and not errors:
        items = [quiet_item(now)]
    return {
        "items": items,
        "region": "Sikkim, Darjeeling, Kalimpong, Siliguri, east Nepal, west Bhutan",
        "radius_km": QUAKE_RADIUS_KM,
        "sources": ["USGS", "GDACS", "Google News"],
        "errors": errors,
        "fetched_at": _iso(now),
    }


def _fetch(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "SIHGIS-bulletin/1.0"})
    with urlopen(req, timeout=20) as resp:
        return resp.read()


def _gather(fetch=_fetch, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    usgs = gdacs = None
    news = None
    start = (now - timedelta(days=QUAKE_DAYS)).strftime("%Y-%m-%d")
    jobs = (
        ("USGS", USGS_URL.format(start=start), "json"),
        ("GDACS", GDACS_URL, "json"),
        ("Google News", NEWS_URL, "xml"),
    )
    payloads: dict[str, object] = {}
    for name, url, kind in jobs:
        try:
            raw = fetch(url)
            payloads[name] = json.loads(raw) if kind == "json" else raw.decode("utf-8", "replace")
        except Exception as e:
            errors.append(f"{name}: {e}")
    if "USGS" in payloads:
        usgs = payloads["USGS"]
    if "GDACS" in payloads:
        gdacs = payloads["GDACS"]
    if "Google News" in payloads:
        news = payloads["Google News"]
    if usgs is None and gdacs is None and news is None:
        raise GateError("; ".join(errors) or "hazard feeds are down")
    return build_bulletin(usgs, gdacs, news, now, errors)


def load_bulletin(fetch=_fetch, now: float | None = None, force: bool = False) -> dict:
    """Return the bulletin. A failed refresh keeps the last good copy."""
    now_s = time.time() if now is None else now
    if not force and _mem["doc"] and now_s - _mem["at"] < CACHE_S:
        return _mem["doc"]
    moment = datetime.fromtimestamp(now_s, tz=timezone.utc)
    try:
        doc = _gather(fetch, moment)
    except Exception as e:
        if _mem["doc"]:
            stale = dict(_mem["doc"])
            stale["stale"] = str(e)
            return stale
        if CACHE_PATH.exists():
            saved = json.loads(CACHE_PATH.read_text())
            saved["stale"] = str(e)
            return saved
        raise GateError(f"hazard feeds are down: {e}") from e
    _mem["at"] = now_s
    _mem["doc"] = doc
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(doc))
    except OSError:
        pass
    return doc
