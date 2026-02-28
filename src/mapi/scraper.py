"""
scraper.py — Scrape API docs with Firecrawl and parse them with Groq.

Two-layer cache per API:
  1. data/{api}/raw.json   — raw markdown from Firecrawl (never re-scrape same URL)
  2. data/{api}/docs.json  — structured endpoints parsed by Groq

If docs.json exists with endpoints → use it (fastest)
If raw.json exists but no docs.json → re-parse with Groq (no Firecrawl needed)
If neither exists → Firecrawl scrapes → save raw.json → Groq parses → save docs.json
"""
import json
import time

import httpx

from .config import DATA_DIR, FIRECRAWL_API_KEY, GROQ_API_KEY, GROQ_MODEL, API_SOURCES
from .display import C


def scrape_url(url: str) -> str:
    """Scrape a single URL with Firecrawl, return markdown."""
    print(f"  {C.DIM}Scraping: {url}{C.RESET}")
    try:
        resp = httpx.post(
            'https://api.firecrawl.dev/v2/scrape',
            headers={
                'Authorization': f'Bearer {FIRECRAWL_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={'url': url, 'formats': ['markdown'], 'onlyMainContent': True},
            timeout=60.0
        )
        if resp.status_code != 200:
            print(f"  {C.RED}✗ Failed ({resp.status_code}){C.RESET}")
            return ''
        md = resp.json().get('data', {}).get('markdown', '')
        print(f"  {C.GREEN}✓ {len(md)} chars{C.RESET}")
        return md
    except Exception as e:
        print(f"  {C.RED}✗ {e}{C.RESET}")
        return ''


def load_raw(api: str) -> dict:
    """Load saved raw markdown from data/{api}/raw.json."""
    raw_file = DATA_DIR / api / 'raw.json'
    if raw_file.exists():
        try:
            with open(raw_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_raw(api: str, raw_data: dict):
    """Save raw markdown to data/{api}/raw.json."""
    out_dir = DATA_DIR / api
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'raw.json', 'w') as f:
        json.dump(raw_data, f, indent=2)


def fetch_raw_markdown(api: str) -> str:
    """
    Get raw markdown for an API.
    Checks raw.json first, only scrapes URLs we don't already have.
    """
    source = API_SOURCES.get(api)
    if not source:
        return ''

    existing_raw = load_raw(api)
    urls_to_scrape = [url for url in source['urls'] if url not in existing_raw]

    # Everything already cached
    if not urls_to_scrape:
        print(f"  {C.GREEN}✓ Using saved scraped data (no Firecrawl needed){C.RESET}")
        all_markdown = ""
        for url in source['urls']:
            all_markdown += f"\n\n--- SOURCE: {url} ---\n\n{existing_raw[url]}"
        return all_markdown

    # Need Firecrawl for some URLs
    if not FIRECRAWL_API_KEY:
        print(f"\n  {C.RED}✗ FIRECRAWL_API_KEY not set{C.RESET}")
        print(f"  Need it to scrape {source['name']} docs.")
        print(f"  Get a free key: {C.CYAN}https://firecrawl.dev{C.RESET}")
        print(f"  Add to .env: FIRECRAWL_API_KEY=fc-your_key\n")
        return ''

    print(f"\n  {C.BOLD}📡 Scraping {len(urls_to_scrape)} new page(s) from {source['name']}...{C.RESET}")
    if existing_raw:
        print(f"  {C.DIM}Already have {len(existing_raw)} page(s) cached.{C.RESET}")
    print()

    for url in urls_to_scrape:
        md = scrape_url(url)
        if md:
            existing_raw[url] = md
        time.sleep(1)

    # Save ALL raw data (old + new)
    save_raw(api, existing_raw)
    print(f"\n  {C.GREEN}✓ Raw data saved — won't need Firecrawl for these pages again{C.RESET}")

    all_markdown = ""
    for url in source['urls']:
        if url in existing_raw:
            all_markdown += f"\n\n--- SOURCE: {url} ---\n\n{existing_raw[url]}"
    return all_markdown


def parse_scraped_docs(markdown: str, api_name: str) -> dict:
    """Send scraped markdown to Groq to extract structured endpoint data."""
    if len(markdown) > 15000:
        markdown = markdown[:15000]

    prompt = f"""Parse this {api_name} API documentation into structured JSON.

Return EXACTLY this format:
{{
  "service_name": "human readable name",
  "auth_type": "API Key",
  "auth_header": "how to pass auth in requests",
  "doc_url": "main docs URL",
  "endpoints": [
    {{
      "action": "short verb phrase like 'geocode address to coordinates'",
      "method": "GET or POST",
      "endpoint": "full URL with path",
      "description": "1-2 sentences",
      "params": "comma-separated list of params with (required) or (optional)"
    }}
  ]
}}

Extract EVERY endpoint. Use real full URLs. Return ONLY valid JSON.

Documentation:
{markdown}"""

    try:
        resp = httpx.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'model': GROQ_MODEL,
                'messages': [{'role': 'user', 'content': prompt}],
                'temperature': 0.1,
                'max_tokens': 4000
            },
            timeout=60.0
        )
        if resp.status_code != 200:
            print(f"  {C.RED}✗ Groq error ({resp.status_code}){C.RESET}")
            return {}
        content = resp.json()['choices'][0]['message']['content'].strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content[:-3]
            content = content.strip()
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"  {C.RED}✗ JSON parse error: {e}{C.RESET}")
        return {}
    except Exception as e:
        print(f"  {C.RED}✗ {e}{C.RESET}")
        return {}


def scrape_api_docs(api: str) -> dict:
    """Full pipeline: get raw markdown → parse with Groq → save docs.json."""
    source = API_SOURCES.get(api)
    if not source:
        return {}

    print(f"\n  {C.BOLD}📡 First time using {source['name']} — building docs...{C.RESET}")
    print(f"  {C.DIM}This only happens once. Results are saved for next time.{C.RESET}")

    all_markdown = fetch_raw_markdown(api)
    if not all_markdown:
        return {}

    print(f"\n  {C.DIM}Parsing docs with AI...{C.RESET}")
    parsed = parse_scraped_docs(all_markdown, source['name'])

    if parsed and parsed.get('endpoints'):
        out_dir = DATA_DIR / api
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / 'docs.json', 'w') as f:
            json.dump(parsed, f, indent=2)
        print(f"  {C.GREEN}✓ Saved {len(parsed['endpoints'])} endpoints{C.RESET}\n")
        return parsed

    print(f"  {C.RED}✗ Failed to parse docs{C.RESET}")
    return {}


def load_api_docs(api: str) -> dict:
    """
    Load docs for an API. Three-level cache:
      1. docs.json has endpoints → return instantly
      2. raw.json exists → re-parse with Groq (no Firecrawl)
      3. nothing → Firecrawl + Groq
    """
    docs_file = DATA_DIR / api / 'docs.json'
    if docs_file.exists():
        try:
            with open(docs_file) as f:
                docs = json.load(f)
            if docs.get('endpoints'):
                return docs
        except (json.JSONDecodeError, ValueError):
            pass
    return scrape_api_docs(api)