"""Refresh Open-Meteo rain/soil on every published risk hotspot.

    python tools/fetch_alerts.py
    python tools/fetch_alerts.py --force
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.alerts import ALERTS_PATH, refresh_alerts
from pipeline.risk_lib import GateError


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="ignore the 30-minute cache")
    args = ap.parse_args()
    try:
        fc = refresh_alerts(force=args.force)
    except GateError as e:
        sys.exit(f"alerts refresh failed: {e}")
    n = fc.get("meta", {}).get("active", len(fc.get("features", [])))
    print(f"active alerts {n}  →  {ALERTS_PATH}")


if __name__ == "__main__":
    main()
