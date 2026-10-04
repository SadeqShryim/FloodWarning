"""Runtime settings, read from environment variables and an optional .env file at the repo root.

Other modules do `from . import config` and read `config.X` when they need it (not
`from .config import X`), so tests can monkeypatch values.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


# Storage
DATA_DIR = Path(_env("FLOODLINE_DATA_DIR") or REPO_ROOT / "data").resolve()
DB_PATH = DATA_DIR / "floodline.db"
UPLOAD_DIR = DATA_DIR / "uploads"
GEOCODE_CACHE_PATH = DATA_DIR / "geocode_cache.json"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
SEED_AUDIO_DIR = REPO_ROOT / "backend" / "seed_audio"
SEED_ON_START = _env("FLOODLINE_SEED", "1") == "1"  # load the demo reports when the DB is empty

# Upload limits
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024

# AI (Gemini through Google AI Studio)
GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in (_env("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-flash-latest") or "").split(",")
    if m.strip()
]
AI_TIMEOUT_S = float(_env("FLOODLINE_AI_TIMEOUT", "15"))  # give up on the AI after this; the report is kept anyway
POST_WAIT_S = float(_env("FLOODLINE_POST_WAIT", "9"))  # how long POST /api/reports waits for the AI before answering

# Public HTTPS address phones use (the tunnel). run.py also sets it at runtime via POST /api/config/public-url.
PUBLIC_URL = _env("PUBLIC_URL")

# Geocoding: OpenStreetMap Nominatim. Policy: at most 1 request/second, identify the app, cache results.
NOMINATIM_URL = _env("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
NOMINATIM_USER_AGENT = _env("NOMINATIM_USER_AGENT", "FloodLine/0.1 (Hack Dearborn 2026 flood-reporting demo)")

# Map defaults: Dearborn, Michigan
MAP_CENTER = (42.3150, -83.1990)
MAP_ZOOM = 13
# Box that biases geocoding to Dearborn: (west, south, east, north)
DEARBORN_VIEWBOX = (-83.3050, 42.2650, -83.1200, 42.3650)
