"""Build the reference-boundary vectors the map draws around Sikkim.

Two files, two jobs:

  borders-world.geojson    every country outline, generalised. This is what the
                           globe shows when you pull out; at that scale anything
                           finer is wasted bytes.
  borders-region.geojson   first-level admin boundaries (Indian states, and the
                           provinces of the neighbours) for a window around
                           Sikkim, at full 1:10m detail.

INDIA'S BOUNDARY
----------------
Both files use Natural Earth's **India point-of-view** edition
(`ne_10m_admin_0_countries_ind`), not the default. The default depicts de-facto
control, which places Pakistan-administered Kashmir, Gilgit-Baltistan and Aksai
Chin outside India. That depiction is not lawful to publish in India, and this
map is presented to an Indian government audience.

The POV edition puts all of them inside India — verified here by point tests at
Gilgit-Baltistan, Aksai Chin and Muzaffarabad, and by India's northern extent
moving from 35.49°N to 37.05°N.

The admin-1 layer needs a second correction. It carries Pakistan's and China's
own provincial divisions across the contested areas, so drawing it unchanged
would paint the Line of Control straight back over a national outline that does
not have one. Those lines are cut, and India's claimed frontier is drawn in
their place at full weight.

Source: Natural Earth (public domain), via nvkelso/natural-earth-vector.
Run:    .venv/bin/python tools/build_borders.py
"""
import json
import pathlib
import subprocess
import tempfile

from shapely.geometry import Point, box, mapping, shape
from shapely.ops import linemerge, unary_union

OUT = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "data"
RAW = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"

# A window wide enough that the neighbours read as context at Sikkim's scale,
# without dragging in the whole of Asia.
REGION = (72.0, 18.0, 100.0, 37.5)          # W, S, E, N

# Every area inside India's claim that Natural Earth draws a separate outline
# around. Cutting all of them is what makes Jammu & Kashmir and Ladakh read as
# one continuous part of India rather than a patchwork of control lines.
#   B08  Gilgit-Baltistan        B09  Pakistan-administered Kashmir
#   B07  Aksai Chin              B06  Shaksgam / Trans-Karakoram
#   B45  Siachen Glacier         — Indian-administered, but NE gives it its own
#                                  outline, which would draw the AGPL
CONTESTED_BRK_A3 = {"B06", "B07", "B08", "B09", "B45"}


def fetch(name: str, tmp: pathlib.Path) -> dict:
    path = tmp / name
    if not path.exists():
        subprocess.run(["curl", "-sSL", "-o", str(path), f"{RAW}/{name}"], check=True)
    return json.loads(path.read_text())


def drop_stubs(geom, contested, min_len=0.12, reach=0.6):
    """Cutting lines against the contested areas leaves short dangling remnants.

    They read as stray ticks floating beside the frontier, so anything short that
    still sits near the cut is discarded. Lines elsewhere are untouched, however
    short — small islands are legitimate.
    """
    if geom.geom_type == "LineString":
        return geom
    near = contested.buffer(reach)
    keep = [g for g in geom.geoms
            if g.length >= min_len or not g.intersects(near)]
    return unary_union(keep)


def round_coords(obj, nd=4):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, list):
        return [round_coords(v, nd) for v in obj]
    return obj


def write(geoms, path: pathlib.Path, simplify: float, props: dict):
    out = []
    for geom in geoms:
        g = geom.simplify(simplify, preserve_topology=True)
        if g.is_empty:
            continue
        gj = mapping(g)
        gj["coordinates"] = round_coords(gj["coordinates"])
        out.append({"type": "Feature", "properties": props, "geometry": gj})
    doc = {"type": "FeatureCollection", "features": out}
    path.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"  {path.name:26s} {len(out):5d} features  {path.stat().st_size / 1024:7.0f} KB")


def check_india(pov):
    """Fail loudly rather than ship a boundary that is wrong for this audience."""
    probes = {
        "Gilgit-Baltistan": (75.0, 36.0),
        "Aksai Chin": (79.0, 35.0),
        "PoK (Muzaffarabad)": (73.6, 34.2),
    }
    for name, pt in probes.items():
        if not pov.contains(Point(*pt)):
            raise SystemExit(f"India POV check FAILED: {name} is outside India")
        print(f"    ✓ {name} inside India")
    print(f"    ✓ northern extent {pov.bounds[3]:.2f}°N")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)

        print("countries — India point of view")
        pov = fetch("ne_10m_admin_0_countries_ind.geojson", tmp)
        india = next((shape(f["geometry"]) for f in pov["features"]
                      if f["properties"].get("ADMIN") == "India"), None)
        if india is None:
            raise SystemExit("India not found in the POV countries file")
        check_india(india)

        disputed = fetch("ne_10m_admin_0_disputed_areas.geojson", tmp)
        contested = unary_union([
            shape(f["geometry"]) for f in disputed["features"]
            if f["properties"].get("BRK_A3") in CONTESTED_BRK_A3
        ])

        # The world layer starts from the 1:110m edition, which is separately
        # generalised by hand — simplifying the 10m source down to the same size
        # gives a visibly worse line (406 KB and still coarser than 110m's 225 KB).
        # So take 110m and repair India in it, rather than rebuild the planet.
        world = fetch("ne_110m_admin_0_countries.geojson", tmp)
        w_lines = [shape(f["geometry"]).boundary for f in world["features"]]
        w = linemerge(unary_union(w_lines))
        # Cut the de-facto lines through the contested areas — at 110m these are
        # India's, Pakistan's and China's outlines all meeting along the LoC —
        # then draw India's claimed frontier in their place.
        # A wide cut on purpose. The 1:110m line sits up to ~25 km off the 1:10m
        # contested polygons, so a tight buffer leaves long diagonal remnants of
        # the de-facto boundary lying across Kashmir. Whatever this over-removes
        # is put back by the claimed frontier below.
        w = w.difference(contested.buffer(0.35))
        w_frontier = india.boundary.intersection(contested.buffer(0.55)).simplify(0.02)
        if not w_frontier.is_empty:
            w = unary_union([w, w_frontier])
        w = drop_stubs(w, contested, min_len=0.3)
        write([w], OUT / "borders-world.geojson", simplify=0.05, props={"kind": "country"})

        print(f"admin-1 — window {REGION}")
        clip = box(*REGION)
        states = fetch("ne_10m_admin_1_states_provinces.geojson", tmp)
        parts = []
        for f in states["features"]:
            g = shape(f["geometry"])
            if not g.intersects(clip):
                continue
            # .boundary first, then clip. Clipping the polygon and taking its
            # boundary afterwards would draw the window rectangle as a border.
            b = g.boundary.intersection(clip)
            if b and not b.is_empty:
                parts.append(b)

        # linemerge matters far more than the simplify tolerance: clipping leaves
        # thousands of short disjoint pieces, and simplify(preserve_topology) will
        # not move their endpoints. Joining contiguous runs first took this file
        # from 1,066 KB to 178 KB at identical detail.
        merged = linemerge(unary_union(parts))

        # Cut every provincial line through the contested areas, and the shared
        # edges with them — that edge IS the Line of Control, and drawing it would
        # contradict the outline we just wrote.
        merged = merged.difference(contested.buffer(0.02))

        # Put India's claimed frontier back where those cuts removed it, so the
        # north-west stays a drawn border rather than a gap.
        frontier = india.boundary.intersection(contested.buffer(0.08))
        if not frontier.is_empty:
            merged = unary_union([merged, frontier])
        print(f"    ✓ {len(CONTESTED_BRK_A3)} contested areas cleared, claimed frontier restored")

        merged = drop_stubs(merged, contested)
        write([linemerge(merged) if merged.geom_type != "LineString" else merged],
              OUT / "borders-region.geojson", simplify=0.008, props={"kind": "admin1"})


if __name__ == "__main__":
    main()
