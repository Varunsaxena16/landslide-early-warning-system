"""XYZ tile rendering straight off the harmonised COGs.

No pre-generated tile pyramid: rio-tiler reads the 512 px block it needs (and
the right overview level for the zoom) and renders a PNG. That keeps the repo
small, makes every zoom level sharp, and means a re-run of the pipeline is
immediately live with no tile-cache invalidation.

Colours come from config/layers.yml, so a palette change is a config edit.
"""
from __future__ import annotations

import struct
import zlib
from functools import lru_cache
from typing import Optional

import numpy as np
from rio_tiler.colormap import cmap as rio_cmap
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

from backend import registry

def _blank_png() -> bytes:
    """A 1x1 fully transparent PNG, built rather than pasted.

    Served for every tile that falls outside a layer - which at low zoom is most
    of them. Worth constructing: a hand-copied byte string with one wrong CRC or
    chunk length decodes nowhere and shows up only as a console error.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)   # 1x1, 8-bit RGBA
    idat = zlib.compress(b"\x00" + b"\x00" * 4, 9)        # filter byte + one clear pixel
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


TRANSPARENT_PNG = _blank_png()


def _hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


@lru_cache(maxsize=256)
def _categorical_colormap(layer_id: str, keep: Optional[tuple] = None) -> dict:
    """{stored_value: (r, g, b, a)} - rio-tiler applies this to raw pixel values.

    `keep` limits which classes are painted; everything else renders fully
    transparent. That is what lets the dashboard's category rail isolate a single
    risk class without a second copy of the raster.
    """
    lyr = registry.layer(layer_id)
    out = {}
    for c in lyr["classes"]:
        visible = keep is None or c["value"] in keep
        out[c["value"]] = (*_hex_to_rgb(c["color"]), 255 if visible else 0)
    return out


# Population is skewed: half of occupied pixels are under 2 people, so a
# cream-to-red bar spends almost all of its ink on yellow. These stops stay
# saturated from the first person and add a second hue at the dense end.
_POP_STOPS = (
    (0.00, "e6550d"),
    (0.18, "d94701"),
    (0.36, "cb181d"),
    (0.54, "980043"),
    (0.72, "6a51a3"),
    (1.00, "3f007d"),
)


def _ramp_colormap(stops: tuple) -> dict:
    pts = [(float(t), _hex_to_rgb(h)) for t, h in stops]
    out = {}
    for i in range(256):
        t = i / 255
        lo, hi = pts[0], pts[-1]
        for a, b in zip(pts, pts[1:]):
            if t <= b[0]:
                lo, hi = a, b
                break
        span = hi[0] - lo[0] or 1.0
        u = min(1.0, max(0.0, (t - lo[0]) / span))
        rgb = tuple(int(round(a + (b - a) * u)) for a, b in zip(lo[1], hi[1]))
        out[i] = (*rgb, 255)
    return out


@lru_cache(maxsize=64)
def _continuous_colormap(name: str) -> dict:
    if name == "pop":
        return _ramp_colormap(_POP_STOPS)
    try:
        return rio_cmap.get(name)
    except Exception:
        return rio_cmap.get("viridis")


def _stretch_array(img, lo: float, hi: float, stretch: str) -> tuple[float, float]:
    """Log stretch puts the common 2–8 people in the coloured middle of the bar."""
    if stretch != "log" or lo <= 0 or hi <= lo:
        return lo, hi
    arr = np.clip(img.array.astype("float64"), lo, hi)
    img.array = np.log(arr)
    return float(np.log(lo)), float(np.log(hi))


def _stored_range(lyr: dict, vmin: float, vmax: float) -> tuple:
    """Display range given in real units -> the stored integer range."""
    s = lyr["store"]
    lo = (vmin - s["offset"]) / s["scale"]
    hi = (vmax - s["offset"]) / s["scale"]
    return (min(lo, hi), max(lo, hi))


@lru_cache(maxsize=512)
def cached_png(layer_id: str, z: int, x: int, y: int, tilesize: int = 256,
               vmin: Optional[float] = None, vmax: Optional[float] = None,
               colormap_name: Optional[str] = None,
               classes: Optional[tuple] = None) -> bytes:
    """Same tile twice should not read the GeoTIFF twice.

    A view asks for a few dozen tiles, and a small host renders them one at a
    time. Keeping the PNG lets a pan back, or a layer toggled off and on,
    return immediately.
    """
    png = render_tile(layer_id, z, x, y, tilesize, vmin, vmax, colormap_name, classes)
    return png if png is not None else TRANSPARENT_PNG


def render_tile(layer_id: str, z: int, x: int, y: int, tilesize: int = 256,
                vmin: Optional[float] = None, vmax: Optional[float] = None,
                colormap_name: Optional[str] = None,
                classes: Optional[tuple] = None) -> Optional[bytes]:
    """One PNG tile, or None when the tile lies outside the layer."""
    lyr = registry.layer(layer_id)
    if lyr is None:
        raise KeyError(layer_id)

    with Reader(lyr["path"]) as src:
        try:
            img = src.tile(x, y, z, tilesize=tilesize, resampling_method="nearest")
        except TileOutsideBounds:
            return None

        if not img.mask.any():
            return None

        if lyr["kind"] == "categorical":
            return img.render(img_format="PNG",
                              colormap=_categorical_colormap(layer_id, classes))

        disp = lyr.get("display", {})
        lo, hi = _stored_range(lyr,
                              float(vmin if vmin is not None else disp.get("min", 0)),
                              float(vmax if vmax is not None else disp.get("max", 1)))
        lo, hi = _stretch_array(img, lo, hi, disp.get("stretch") or "")
        img.rescale(in_range=((lo, hi),), out_range=((0, 255),))
        name = colormap_name or disp.get("colormap", "viridis")
        return img.render(img_format="PNG", colormap=_continuous_colormap(name))


def legend(layer_id: str, steps: int = 24) -> dict:
    """Everything the sidebar needs to draw this layer's legend."""
    lyr = registry.layer(layer_id)
    if lyr is None:
        raise KeyError(layer_id)

    if lyr["kind"] == "categorical":
        return {"id": layer_id, "kind": "categorical", "title": lyr["title"],
                "labels_inferred": lyr["labels_inferred"],
                "entries": [{"label": c["label"], "color": c["color"],
                             "value": c["value"], "inferred": c["inferred"],
                             "area_km2": c["area_km2"]} for c in lyr["classes"]]}

    disp = lyr.get("display", {})
    vmin, vmax = float(disp.get("min", 0)), float(disp.get("max", 1))
    cm = _continuous_colormap(disp.get("colormap", "viridis"))
    ramp = []
    for i in range(steps):
        t = i / (steps - 1)
        rgba = cm[int(round(t * 255))]
        ramp.append({"t": round(t, 3),
                     "color": "#%02x%02x%02x" % tuple(int(c) for c in rgba[:3]),
                     "value": round(vmin + t * (vmax - vmin), 4)})
    return {"id": layer_id, "kind": "continuous", "title": lyr["title"],
            "units": lyr.get("units"), "min": vmin, "max": vmax,
            "stats": lyr.get("stats"), "ramp": ramp}
