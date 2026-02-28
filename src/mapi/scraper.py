"""
scraper.py — Scrape API docs with Firecrawl and parse them with Groq.

This runs the FIRST TIME a user queries an API that hasn't been scraped yet.
After scraping, the structured docs are saved to data/{api}/docs.json.
Next time, the saved file is loaded instantly — no scraping needed.

Key design: each doc URL is parsed SEPARATELY so Groq gets focused chunks
instead of one massive blob that gets truncated.
"""
import json
import time
from pathlib import Path

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
    """Scrape all doc pages for an API, parse each one separately, merge, save."""
    source = API_SOURCES.get(api)
    if not source:
        return {}

    if not FIRECRAWL_API_KEY:
        print(f"\n  {C.RED}✗ FIRECRAWL_API_KEY not set{C.RESET}")
        print(f"  Need it to scrape {source['name']} docs.")
        print(f"  Get a free key: {C.CYAN}https://firecrawl.dev{C.RESET}")
        print(f"  Add to .env: FIRECRAWL_API_KEY=fc-your_key\n")
        return {}

    print(f"\n  {C.BOLD}📡 First time using {source['name']} — scraping docs...{C.RESET}")
    print(f"  {C.DIM}This only happens once. Results are saved for next time.{C.RESET}\n")

    all_endpoints = []
    for url in source['urls']:
        md = scrape_url(url)
        if not md:
            time.sleep(1)
            continue

        print(f"  {C.DIM}Parsing: {url}...{C.RESET}")
        endpoints = parse_single_doc(md, source['name'], url)
        if endpoints:
            print(f"  {C.GREEN}✓ {len(endpoints)} endpoints{C.RESET}")
            all_endpoints.extend(endpoints)
        else:
            print(f"  {C.DIM}  (no endpoints found){C.RESET}")
        time.sleep(1)

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
        # This catches both models/gemini-flash:generateContent and models:generateContent
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
    Load docs for an API.
    If data/{api}/docs.json exists and has endpoints → return it.
    If not → scrape with Firecrawl, save, then return.
    """
    docs_file = DATA_DIR / api / 'docs.json'
    if docs_file.exists():
        with open(docs_file) as f:
            docs = json.load(f)
        if docs.get('endpoints'):
            return docs
    return scrape_api_docs(api)
