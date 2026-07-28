"""
config.py — All settings, API keys, paths, and hardcoded doc URLs.
"""
import os
from pathlib import Path

from dotenv import load_dotenv, find_dotenv

# Load .env robustly, no matter where `mapi` is invoked from:
#   1. The project root shipped alongside this source (src/mapi/config.py -> root).
#   2. Any .env found by walking up from the current working directory.
# Real environment variables always win; .env only fills in what's missing.
_PROJECT_ENV = Path(__file__).resolve().parents[2] / '.env'
if _PROJECT_ENV.exists():
    load_dotenv(_PROJECT_ENV, override=False)
_cwd_env = find_dotenv(usecwd=True)
if _cwd_env:
    load_dotenv(_cwd_env, override=False)

# ── Paths ───────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / 'data'
LAST_RESULT_PATH = DATA_DIR / 'last_result.json'

# ── API Keys ────────────────────────────────────────────────────────
FIRECRAWL_API_KEY = os.environ.get('FIRECRAWL_API_KEY', '')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = 'llama-3.3-70b-versatile'

# ── Hardcoded doc URLs per API ──────────────────────────────────────
# First time a user queries an API, we scrape these pages with Firecrawl.
# After that, the scraped data is saved locally and reused.
API_SOURCES = {
    "googlemaps": {
        "name": "Google Maps Platform",
        "auth": "Pass API key as query parameter: ?key=YOUR_API_KEY",
        "urls": [
            "https://developers.google.com/maps/documentation/geocoding/requests-geocoding",
            "https://developers.google.com/maps/documentation/directions/get-directions",
            "https://developers.google.com/maps/documentation/places/web-service/nearby-search",
            "https://developers.google.com/maps/documentation/places/web-service/place-details",
            "https://developers.google.com/maps/documentation/distancematrix/distance-matrix",
        ]
    },
    "gemini": {
        "name": "Google Gemini API",
        "auth": "Pass API key as header: x-goog-api-key: YOUR_API_KEY",
        "urls": [
            "https://ai.google.dev/gemini-api/docs/quickstart",
            "https://ai.google.dev/api/generate-content",
            "https://ai.google.dev/api/embeddings",
            "https://ai.google.dev/api/models",
        ]
    },
    "jobnimbus": {
        "name": "JobNimbus Public API",
        "auth": "Pass API key as header: Authorization: bearer YOUR_API_KEY",
        # Postman-published docs. The documenter page is JS-rendered, so we pull
        # the underlying collection JSON directly and convert it (see postman.py).
        "postman_url": "https://documenter.gw.postman.com/api/collections/3919598/S11PpG4x?segregateAuth=true&versionTag=latest",
        "doc_url": "https://documenter.getpostman.com/view/3919598/S11PpG4x",
        "urls": [],
    },
}

# ── Aliases ─────────────────────────────────────────────────────────
# Short names / synonyms that resolve to a real API key in API_SOURCES.
API_ALIASES = {
    "jn": "jobnimbus",
    "maps": "googlemaps",
    "gmaps": "googlemaps",
}


def resolve_api(name: str) -> str:
    """Resolve a user-typed API name through aliases (case-insensitive)."""
    name = (name or '').lower()
    return API_ALIASES.get(name, name)