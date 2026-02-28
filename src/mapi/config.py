"""
config.py — All settings, API keys, paths, and hardcoded doc URLs.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

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
}