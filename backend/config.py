"""Runtime settings. Everything is overridable by environment variable so the
container and the local dev server share one code path."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = Path(os.environ.get("PROCESSED_DIR", ROOT / "data" / "processed"))
COG_DIR = PROCESSED_DIR / "cog"
VEC_DIR = PROCESSED_DIR / "vector"
CONFIG_FILE = Path(os.environ.get("LAYERS_CONFIG", ROOT / "config" / "layers.yml"))
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", ROOT / "frontend"))
PLACES_FILE = Path(os.environ.get("PLACES_FILE", ROOT / "reference" / "places_sikkim.csv"))

TILE_SIZE = int(os.environ.get("TILE_SIZE", 256))
TILE_MAX_ZOOM = int(os.environ.get("TILE_MAX_ZOOM", 16))
CORS_ORIGINS = [o for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o]
API_PREFIX = "/api"
