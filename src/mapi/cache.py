"""
cache.py — Read and write the query result cache.

Each API has its own cache file: data/{api}/cache.json
Cache key format: "python:generate text" (API name implied by folder)
Exact key match = instant result, no Groq call needed.
"""
import json
from pathlib import Path

from .config import DATA_DIR


def get_cache_path(api: str) -> Path:
    """Get cache file path for a specific API."""
    api_dir = DATA_DIR / api
    api_dir.mkdir(parents=True, exist_ok=True)
    return api_dir / 'cache.json'


def load_cache(api: str) -> dict:
    """Load cache for a specific API."""
    cache_path = get_cache_path(api)
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_cache(api: str, cache: dict):
    """Save cache for a specific API."""
    cache_path = get_cache_path(api)
    with open(cache_path, 'w') as f:
        json.dump(cache, f, indent=2)
