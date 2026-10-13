"""Pull Sikkim highways and assembly points from OpenStreetMap.

Writes clipped GeoJSON the early-warning panel scores against the published
risk polygons and the GSI inventory. Nothing here is inferred from the
town list.

    python tools/fetch_society.py

Source: OpenStreetMap contributors, via the Overpass API. ODbL.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "processed" / "society"
OUTLINE = ROOT / "frontend" / "data" / "sikkim.geojson"
# south, west, north, east — Overpass order
BBOX = "27.073148,88.010695,28.134655,88.938677"
ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

HW_QUERY = f"""
[out:json][timeout:180];
(
  way["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link)$"]({BBOX});
);
out geom;
"""

SHELTER_QUERIES = (
    f"""
[out:json][timeout:90];
(
  node["amenity"~"^(school|college|university|community_centre|townhall)$"]({BBOX});
  node["leisure"~"^(stadium|playground|pitch)$"]({BBOX});
);
out body;
""",
    f"""
[out:json][timeout:90];
(
  way["amenity"~"^(school|college|university|community_centre|townhall)$"]({BBOX});
  way["leisure"~"^(stadium|playground|pitch)$"]({BBOX});
);
out center;
""",
)

KIND = {
    "school": "school",
    "college": "college",
    "university": "college",
    "community_centre": "community centre",
    "townhall": "town hall",
    "stadium": "stadium",
    "playground": "ground",
    "pitch": "ground",
}


def _post(query: str) -> dict:
    body = urlencode({"data": query}).encode()
    last = None
    for url in ENDPOINTS:
        req = Request(url, data=body, headers={"User-Agent": "SIHGIS/society-fetch"})
        try:
            with urlopen(req, timeout=200) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 — try the next mirror
            last = e
            print(f"overpass failed {url}: {e}", file=sys.stderr)
    raise SystemExit(f"Overpass unavailable: {last}")


def _outline():
    fc = json.loads(OUTLINE.read_text())
    geoms = [shape(f["geometry"]) for f in fc["features"]]
    return unary_union(geoms)


def _clip_lines(elements, outline) -> list[dict]:
    features = []
    for el in elements:
        if el.get("type") != "way":
            continue
        coords = [(p["lon"], p["lat"]) for p in el.get("geometry") or []]
        if len(coords) < 2:
            continue
        line = LineString(coords)
        if line.is_empty or not line.intersects(outline):
            continue
        piece = line.intersection(outline)
        tags = el.get("tags") or {}
        parts = list(piece.geoms) if piece.geom_type.startswith("Multi") else [piece]
        for part in parts:
            if part.is_empty or part.geom_type != "LineString" or part.length == 0:
                continue
            features.append({
                "type": "Feature",
                "properties": {
                    "osm_id": el.get("id"),
                    "name": tags.get("name") or tags.get("name:en") or "",
                    "ref": tags.get("ref") or "",
                    "highway": tags.get("highway") or "",
                },
                "geometry": mapping(part),
            })
    return features


def _points(elements, outline) -> list[dict]:
    features = []
    for el in elements:
        tags = el.get("tags") or {}
        if el.get("type") == "node":
            lon, lat = el.get("lon"), el.get("lat")
        else:
            c = el.get("center") or {}
            lon, lat = c.get("lon"), c.get("lat")
        if lon is None or lat is None:
            continue
        if not outline.covers(Point(lon, lat)):
            continue
        amenity = tags.get("amenity") or ""
        leisure = tags.get("leisure") or ""
        kind = KIND.get(amenity) or KIND.get(leisure)
        if not kind:
            continue
        name = tags.get("name") or tags.get("name:en") or ""
        features.append({
            "type": "Feature",
            "properties": {
                "osm_id": el.get("id"),
                "name": name,
                "kind": kind,
                "amenity": amenity,
                "leisure": leisure,
            },
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
        })
    return features


def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    if not OUTLINE.exists():
        raise SystemExit(f"missing {OUTLINE}")
    outline = _outline()
    OUT.mkdir(parents=True, exist_ok=True)
    fetched = datetime.now(timezone.utc).isoformat()

    if only != "shelters":
        print("fetching highways…")
        hw = _post(HW_QUERY)
        roads = _clip_lines(hw.get("elements") or [], outline)
        hw_path = OUT / "highways.geojson"
        hw_path.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": roads,
            "meta": {
                "source": "OpenStreetMap",
                "licence": "ODbL",
                "via": ENDPOINTS[0],
                "fetched_at": fetched,
                "classes": "motorway, trunk, primary, secondary (and their links)",
                "count": len(roads),
            },
        }))
        print(f"  {len(roads)} ways inside Sikkim → {hw_path}")

    if only == "highways":
        return
    print("fetching schools, grounds, halls…")
    elements = []
    for q in SHELTER_QUERIES:
        elements.extend(_post(q).get("elements") or [])
    points = _points(elements, outline)
    sh_path = OUT / "assembly_raw.geojson"
    sh_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": points,
        "meta": {
            "source": "OpenStreetMap",
            "licence": "ODbL",
            "via": ENDPOINTS[0],
            "fetched_at": fetched,
            "kinds": "school, college, community centre, town hall, stadium, playground, pitch",
            "count": len(points),
        },
    }))
    print(f"  {len(points)} points inside Sikkim → {sh_path}")


if __name__ == "__main__":
    main()
