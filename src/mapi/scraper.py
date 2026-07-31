"""
scraper.py — Scrape API docs with Firecrawl and parse them with Groq.

Two-layer cache per API:
  1. data/{api}/raw.json   — raw markdown from Firecrawl (never re-scrape same URL)
  2. data/{api}/docs.json  — structured endpoints parsed by Groq

Key design: each doc URL is parsed SEPARATELY so Groq gets focused chunks
instead of one massive blob that gets truncated.

If docs.json exists with endpoints → use it (fastest)
If raw.json exists but no docs.json → re-parse with Groq (no Firecrawl needed)
If neither exists → Firecrawl scrapes → save raw.json → Groq parses → save docs.json
"""
import json
import time

import httpx

from .config import DATA_DIR, FIRECRAWL_API_KEY, GROQ_API_KEY, GROQ_MODEL, API_SOURCES
from .display import C

MAX_CHARS_PER_URL = 12000


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


def fetch_raw_markdown(api: str) -> dict:
    """
    Get raw markdown for an API, keyed by URL.
    Checks raw.json first, only scrapes URLs we don't already have.
    Returns dict {url: markdown} so each page can be parsed separately.
    """
    source = API_SOURCES.get(api)
    if not source:
        return {}

    existing_raw = load_raw(api)
    urls_to_scrape = [url for url in source['urls'] if url not in existing_raw]

    # Everything already cached
    if not urls_to_scrape:
        print(f"  {C.GREEN}✓ Using saved scraped data (no Firecrawl needed){C.RESET}")
        return {url: existing_raw[url] for url in source['urls'] if url in existing_raw}

    # Need Firecrawl for some URLs
    if not FIRECRAWL_API_KEY:
        print(f"\n  {C.RED}✗ FIRECRAWL_API_KEY not set{C.RESET}")
        print(f"  Need it to scrape {source['name']} docs.")
        print(f"  Get a free key: {C.CYAN}https://firecrawl.dev{C.RESET}")
        print(f"  Add to .env: FIRECRAWL_API_KEY=fc-your_key\n")
        return {}

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

    return {url: existing_raw[url] for url in source['urls'] if url in existing_raw}


def parse_single_doc(markdown: str, api_name: str, url: str) -> list:
    """Parse ONE doc page into a list of endpoint dicts."""
    if len(markdown) > MAX_CHARS_PER_URL:
        markdown = markdown[:MAX_CHARS_PER_URL]

    prompt = f"""Parse this {api_name} API documentation page into structured JSON.

Return a JSON array of endpoints found on this page. For each endpoint return:
{{
  "action": "short verb phrase like 'generate text content'",
  "method": "GET or POST",
  "endpoint": "full URL with path",
  "description": "1-2 sentences",
  "params": "comma-separated list of params with (required) or (optional)",
  "example_body": {{}} // For POST endpoints ONLY: a complete, valid JSON request body that would work. Copy the exact structure from the docs. For GET endpoints, use null.
}}

IMPORTANT:
- Copy request body structures EXACTLY from the documentation. Do not simplify or flatten them.
- Use real full URLs (e.g. https://generativelanguage.googleapis.com/v1beta/...).
- If the docs show the body needs nested arrays/objects, include that nesting in example_body.
- Return ONLY a valid JSON array. No explanation.

Documentation from {url}:
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
            return []
        content = resp.json()['choices'][0]['message']['content'].strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content[:-3]
            content = content.strip()
        result = json.loads(content)
        if isinstance(result, dict) and 'endpoints' in result:
            result = result['endpoints']
        if isinstance(result, list):
            return result
        return []
    except json.JSONDecodeError as e:
        print(f"  {C.RED}✗ JSON parse error: {e}{C.RESET}")
        return []
    except Exception as e:
        print(f"  {C.RED}✗ {e}{C.RESET}")
        return []


def scrape_api_docs(api: str) -> dict:
    """Full pipeline: get raw markdown → parse each page with Groq → save docs.json."""
    source = API_SOURCES.get(api)
    if not source:
        return {}

    # OpenAPI-specced APIs: convert the spec straight to docs.json.
    if source.get('openapi_url'):
        from .openapi import load_docs_from_openapi
        return load_docs_from_openapi(api)

    # Postman-published APIs: pull the collection JSON instead of Firecrawl.
    if source.get('postman_url'):
        from .postman import load_docs_from_postman
        return load_docs_from_postman(api)

    print(f"\n  {C.BOLD}📡 First time using {source['name']} — building docs...{C.RESET}")
    print(f"  {C.DIM}This only happens once. Results are saved for next time.{C.RESET}\n")

    # Step 1: Get raw markdown per URL (uses raw.json cache)
    raw_by_url = fetch_raw_markdown(api)
    if not raw_by_url:
        return {}

    # Step 2: Parse each page separately with Groq
    all_endpoints = []
    for url, md in raw_by_url.items():
        if not md:
            continue
        print(f"  {C.DIM}Parsing: {url}...{C.RESET}")
        endpoints = parse_single_doc(md, source['name'], url)
        if endpoints:
            print(f"  {C.GREEN}✓ {len(endpoints)} endpoints{C.RESET}")
            all_endpoints.extend(endpoints)
        else:
            print(f"  {C.DIM}  (no endpoints found){C.RESET}")

    if not all_endpoints:
        print(f"  {C.RED}✗ Could not extract any endpoints{C.RESET}")
        return {}

    # Deduplicate: prefer endpoints with concrete URLs (no {model=...} templates)
    # and prefer entries that appeared first (quickstart > reference pages)
    seen_actions = {}
    unique = []
    for ep in all_endpoints:
        url = ep.get('endpoint', '')
        if not url:
            continue
        # Skip template URLs like /v1beta/{model=models/*}:embedContent
        if '{' in url:
            continue
        # Deduplicate by the action suffix (e.g. :generateContent)
        action_key = url.split(':')[-1] if ':' in url.split('/')[-1] else url.rsplit('/', 1)[-1]
        if action_key not in seen_actions:
            seen_actions[action_key] = True
            unique.append(ep)

    parsed = {
        'service_name': source['name'],
        'auth': source.get('auth', 'See documentation'),
        'doc_url': source['urls'][0],
        'endpoints': unique,
    }

    out_dir = DATA_DIR / api
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'docs.json', 'w') as f:
        json.dump(parsed, f, indent=2)
    print(f"\n  {C.GREEN}✓ Saved {len(unique)} endpoints total{C.RESET}\n")
    return parsed


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
