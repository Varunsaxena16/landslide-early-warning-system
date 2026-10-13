"""Run the whole pipeline: raw delivery -> everything the dashboard needs.

    python -m pipeline.run_all            # all steps
    python -m pipeline.run_all --from 3   # resume from step 3
    python -m pipeline.run_all --only 5   # just re-run the statistics

Steps 2-5 are ordered by dependency; step 1 is a read-only report.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline import (s01_inventory, s02_harmonise, s03_classify, s04_inventory_points, s05_stats)
from pipeline.common import OUT_DIR, RAW_DIR

STEPS = [
    (1, "inventory raw delivery", s01_inventory.main),
    (2, "harmonise onto the analysis grid", s02_harmonise.main),
    (3, "classify + build hotspots", s03_classify.main),
    (4, "GSI inventory points", s04_inventory_points.main),
    (5, "statistics and validation", s05_stats.main),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=int, default=1, help="first step to run")
    ap.add_argument("--only", type=int, help="run a single step")
    args = ap.parse_args()

    if not RAW_DIR.exists():
        sys.exit(f"raw data directory not found: {RAW_DIR}\n"
                 f"Set RAW_DIR, or see misc/docs/DATA.md for where to get the delivery.")

    steps = [s for s in STEPS if (s[0] == args.only if args.only else s[0] >= args.start)]
    t0 = time.time()
    for num, name, fn in steps:
        t = time.time()
        fn()
        print(f"\n  ✓ step {num} ({name}) in {time.time() - t:.1f}s")

    total_mb = sum(p.stat().st_size for p in OUT_DIR.rglob("*") if p.is_file()) / 1e6
    print(f"\n\033[1m{'=' * 78}\nPIPELINE COMPLETE in {time.time() - t0:.1f}s — "
          f"{total_mb:.1f} MB in {OUT_DIR}\n{'=' * 78}\033[0m")
    print("\nNext:  make serve      →  http://localhost:8000\n")


if __name__ == "__main__":
    main()
