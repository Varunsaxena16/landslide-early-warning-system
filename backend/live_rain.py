"""Live rainfall frames for the early-warning map.

The tiles are not stored here. The catalog is a short list of timestamps.

RainViewer’s free radar composite was checked on Sikkim (27.3–27.9°N,
88.3–88.6°E) and the tiles are effectively empty: ground radar does not
cover this part of the Himalaya. LibreWXR serves the same tile shape from
NOAA RRQPE observed precipitation, plus a short nowcast, with no API key.
That catalog is what the dashboard animates. Licence: CC-BY-4.0, attribute
LibreWXR.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

from pipeline.common import OUT_DIR
from pipeline.risk_lib import GateError

CATALOG_URL = "https://api.librewxr.net/public/weather-maps.json"
CACHE_PATH = OUT_DIR / "risk" / "live_rain.json"
CACHE_S = 120

ATTRIBUTION = "LibreWXR (CC-BY-4.0)"
SOURCE = (
    "LibreWXR live tiles, https://librewxr.net — NOAA RRQPE observed "
    "precipitation plus a short nowcast, Universal Blue colours (light cyan "
    "to yellow). No API key. RainViewer’s free ground-radar composite was "
    "checked over Sikkim and does not cover it, so it is not the feed. "
    "A clear patch means this hour has no rain in the product. The clock on "
    "the row is the frame time."
)

_mem: dict = {"at": 0.0, "doc": None}


def frames_from_catalog(doc: dict) -> dict:
    host = str(doc.get("host") or "").rstrip("/")
    radar = doc.get("radar") or {}
    frames = []
    for kind in ("past", "nowcast"):
        for row in radar.get(kind) or []:
            path = row.get("path")
            when = row.get("time")
            if not path or when is None:
                continue
            frames.append({"time": int(when), "path": str(path), "kind": kind})
    if not host or not frames:
        raise GateError("live rain catalog has no frames")
    frames.sort(key=lambda f: (f["time"], f["kind"] != "past"))
    return {
        "host": host,
        "frames": frames,
        "tile": "{host}{path}/256/{z}/{x}/{y}/2/1_1.png",
        "max_zoom": 6,
        "attribution": ATTRIBUTION,
        "source": SOURCE,
        "fetched_at": None,
    }


def _fetch(url: str = CATALOG_URL) -> dict:
    req = Request(url, headers={"User-Agent": "PahiroWatch-live-rain/1.0"})
    with urlopen(req, timeout=25) as resp:
        return json.load(resp)


def load_catalog(fetch=_fetch, now: float | None = None, force: bool = False) -> dict:
    """Return the frame list. A failed refresh keeps the last good catalog."""
    now = time.time() if now is None else now
    if not force and _mem["doc"] and now - _mem["at"] < CACHE_S:
        return _mem["doc"]
    try:
        doc = frames_from_catalog(fetch())
    except Exception as e:
        if _mem["doc"]:
            stale = dict(_mem["doc"])
            stale["stale"] = str(e)
            return stale
        if CACHE_PATH.exists():
            saved = json.loads(CACHE_PATH.read_text())
            saved["stale"] = str(e)
            return saved
        raise GateError(f"live rain feed is down: {e}") from e
    doc["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    _mem["at"] = now
    _mem["doc"] = doc
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(doc))
    except OSError:
        pass
    return doc
