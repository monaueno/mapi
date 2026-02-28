"""
scraper.py — Scrape API docs with Firecrawl and parse them with Groq.

This runs the FIRST TIME a user queries an API that hasn't been scraped yet.
After scraping, the structured docs are saved to data/{api}/docs.json.
Next time, the saved file is loaded instantly — no scraping needed.
"""
import json
import time
from pathlib import Path

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
    """Scrape all doc pages for an API, parse them, save to disk."""
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

    all_markdown = ""
    for url in source['urls']:
        md = scrape_url(url)
        if md:
            all_markdown += f"\n\n--- SOURCE: {url} ---\n\n{md}"
        time.sleep(1)

    if not all_markdown:
        print(f"  {C.RED}✗ Could not scrape any pages{C.RESET}")
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