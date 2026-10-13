"""Write the early-warning surfaces into data/processed/cog/.

    python tools/fetch_surfaces.py
    python tools/fetch_surfaces.py --force
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.surfaces import refresh_surfaces


def main() -> None:
    doc = refresh_surfaces(force="--force" in sys.argv)
    fc = doc.get("forecast") or {}
    rain = fc.get("rain_24h") or {}
    soil = fc.get("soil_moisture") or {}
    pop = doc.get("population_stats") or {}
    print(
        f"surfaces  rain {rain.get('min')}–{rain.get('max')} mm   "
        f"soil {soil.get('min')}–{soil.get('max')}   "
        f"people pixels {pop.get('pixels')}"
    )


if __name__ == "__main__":
    main()
