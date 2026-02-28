"""
cache.py — Read and write the query result cache.

The cache is a JSON file: { "python:gemini:generate text": { code, endpoint, verification } }
Exact key match = instant result, no Groq call needed.
"""
import json

from .config import CACHE_PATH


def load_cache() -> dict:
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)