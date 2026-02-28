"""
scraper.py — Scrape API docs with Firecrawl and parse them into structured endpoints.

Three-layer cache per API:
  1. data/{api}/raw.json   — raw markdown from Firecrawl (never re-scrape same URL)
  2. data/{api}/docs.json  — structured endpoints (the finished product)
  3. Query cache            — handled separately by cache.py

Parsing strategy:
  API reference pages follow a consistent structure (## Method: ... → ### Endpoint
  → curl examples). We extract endpoints directly from markdown structure + curl
  examples using regex, then only use Groq for a single pass to generate action
  descriptions. This is far more reliable than asking Groq to parse raw docs.

  Tutorial/quickstart pages are less structured, so we extract curl examples
  directly and derive endpoint info from the URLs.
"""
import json
import re
import time

import httpx

from .config import DATA_DIR, FIRECRAWL_API_KEY, GROQ_API_KEY, GROQ_MODEL, API_SOURCES
from .display import C


# ── Firecrawl scraping layer ──────────────────────────────────────────

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
    Returns dict {url: markdown}.
    """
    source = API_SOURCES.get(api)
    if not source:
        return {}

    existing_raw = load_raw(api)
    urls_to_scrape = [url for url in source['urls'] if url not in existing_raw]

    if not urls_to_scrape:
        print(f"  {C.GREEN}✓ Using saved scraped data (no Firecrawl needed){C.RESET}")
        return {url: existing_raw[url] for url in source['urls'] if url in existing_raw}

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

    save_raw(api, existing_raw)
    print(f"\n  {C.GREEN}✓ Raw data saved — won't need Firecrawl for these pages again{C.RESET}")

    return {url: existing_raw[url] for url in source['urls'] if url in existing_raw}


# ── Markdown parsing layer (regex-based, no LLM) ─────────────────────

def _clean_url(url: str) -> str:
    """Strip query params, trailing junk, shell vars from a URL."""
    # Remove ?key=$VAR, ?alt=sse&key=$VAR, etc.
    url = re.sub(r'\?.*$', '', url)
    # Remove trailing filenames that got concatenated (e.g. models.sh)
    url = re.sub(r'[a-z]+\.(sh|py|go|js)$', '', url)
    return url.rstrip('/')


def _parse_curl_body(curl_block: str) -> dict | None:
    """Extract JSON body from a curl -d '...' block."""
    # Match -d '{...}' — the body may span multiple lines with backslash continuations
    m = re.search(r"-d\s+'(.*?)'", curl_block, re.DOTALL)
    if not m:
        return None
    body_str = m.group(1)
    # Remove shell line continuations and trailing junk after the JSON
    body_str = body_str.replace('\\\n', '').replace('\\', '')
    # Sometimes there's trailing text after the closing brace (e.g. "embed.sh")
    brace_depth = 0
    end_pos = len(body_str)
    for i, ch in enumerate(body_str):
        if ch == '{':
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0:
                end_pos = i + 1
                break
    body_str = body_str[:end_pos]
    # Fix trailing commas before } or ] (common in hand-written examples)
    body_str = re.sub(r',\s*([}\]])', r'\1', body_str)
    try:
        return json.loads(body_str)
    except json.JSONDecodeError:
        return None


def _camel_to_words(name: str) -> str:
    """Convert camelCase to space-separated words: 'generateContent' → 'generate content'."""
    return re.sub(r'([a-z])([A-Z])', r'\1 \2', name).lower()


def extract_endpoints_from_reference(markdown: str) -> list:
    """
    Extract endpoints from an API reference page.
    These pages have consistent structure: ## Method: models.X → ### Endpoint → curl examples.
    """
    endpoints = []
    # Split by "## Method:" sections
    sections = re.split(r'(?=## Method:\s)', markdown)

    for section in sections:
        header = re.match(r'## Method:\s*(\S+)', section)
        if not header:
            continue
        method_name = header.group(1)  # e.g. "models.generateContent"

        # Derive action from method name: "models.generateContent" → "generate content"
        # For simple names like "models.get" → "get model", "models.list" → "list models"
        short_name = method_name.split('.')[-1]  # "generateContent"
        resource = method_name.split('.')[0] if '.' in method_name else ''
        action = _camel_to_words(short_name)      # "generate content"
        # For simple verbs, append the resource name for clarity
        if action in ('get', 'list', 'delete', 'create', 'update'):
            singular = resource.rstrip('s') if resource.endswith('s') else resource
            plural = resource if resource.endswith('s') else resource + 's'
            action = f"{action} {plural}" if action == 'list' else f"{action} {singular}"

        # Get HTTP method from the endpoint block (e.g. "post\n`https://...")
        http_method = 'POST'
        method_match = re.search(r'\b(get|post|put|delete|patch)\s*\n\s*`https://', section, re.IGNORECASE)
        if method_match:
            http_method = method_match.group(1).upper()

        # Get description — find the prose paragraph that describes the method.
        # It's typically the first line after the TOC/nav block that doesn't
        # start with - or # or ` (i.e., actual prose text).
        desc = ''
        lines_after_header = section.split('\n')
        in_body = False
        for line in lines_after_header:
            stripped = line.strip()
            # Skip empty lines, TOC links, headers, code blocks
            if not stripped or stripped.startswith(('-', '#', '`', '|', '[')):
                if in_body:
                    break  # end of prose paragraph
                continue
            # Found a prose line
            in_body = True
            desc_text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', stripped)
            desc = desc_text[:200]
            break

        # Find the FIRST curl example with a concrete API URL
        curl_url = None
        for m in re.finditer(
            r'curl\s+(?:-X\s+\w+\s+)?["\']?(https://generativelanguage\.googleapis\.com/[^\s"\'\\]+)',
            section
        ):
            candidate = _clean_url(m.group(1))
            if '{' not in candidate:  # skip template URLs
                curl_url = candidate
                break

        # Fall back to template URL from the endpoint definition
        if not curl_url:
            tmpl = re.search(r'`(https://generativelanguage\.googleapis\.com/[^`]+)`', section)
            if tmpl:
                curl_url = _clean_url(tmpl.group(1))
            else:
                continue

        # Extract body from the first curl -d block
        body = None
        if http_method == 'POST':
            curl_block_match = re.search(
                r'(curl\s+["\']?https://generativelanguage.*?```)',
                section, re.DOTALL
            )
            if curl_block_match:
                body = _parse_curl_body(curl_block_match.group(1))

        # Extract params from the "Request body" or "Query parameters" section
        params = ''
        params_section = re.search(r'### (?:Request body|Query parameters)(.*?)(?=###|\Z)', section, re.DOTALL)
        if params_section:
            param_names = re.findall(r'`(\w+)`.*?\b(Required|Optional)\b', params_section.group(1), re.IGNORECASE)
            if param_names:
                params = ', '.join(f"{name} ({req.lower()})" for name, req in param_names[:8])

        endpoints.append({
            'action': action,
            'method': http_method,
            'endpoint': curl_url,
            'description': desc or f'{action} using the API',
            'params': params,
            'example_body': body,
        })

    return endpoints


def extract_endpoints_from_tutorial(markdown: str) -> list:
    """
    Extract endpoints from tutorial/quickstart pages.
    Less structured — just find curl examples and derive endpoint info from URLs.
    """
    endpoints = []
    seen_urls = set()

    for m in re.finditer(
        r'curl\s+(?:-X\s+\w+\s+)?["\']?(https://generativelanguage\.googleapis\.com/[^\s"\'\\]+)',
        markdown
    ):
        raw_url = _clean_url(m.group(1))
        if '{' in raw_url or raw_url in seen_urls:
            continue
        if '/files/' in raw_url:  # skip file upload helper URLs
            continue
        seen_urls.add(raw_url)

        # Derive action from URL path: .../models/gemini-2.0-flash:generateContent → generate content
        action_match = re.search(r':(\w+)$', raw_url)
        if action_match:
            action = _camel_to_words(action_match.group(1))
        else:
            # GET endpoints like /v1beta/models
            last_segment = raw_url.rstrip('/').rsplit('/', 1)[-1]
            action = f'list {last_segment}' if '/' in raw_url else last_segment

        # Determine HTTP method: POST if there's a -d body, else GET
        # Look at the full curl block
        block_start = m.start()
        block_end_match = re.search(r'```', markdown[block_start:])
        block_end = block_start + block_end_match.start() if block_end_match else block_start + 1000
        curl_block = markdown[block_start:block_end]

        has_body = "-d '" in curl_block or '-d "' in curl_block
        has_post = '-X POST' in curl_block
        http_method = 'POST' if (has_body or has_post) else 'GET'

        body = _parse_curl_body(curl_block) if has_body else None

        endpoints.append({
            'action': action,
            'method': http_method,
            'endpoint': raw_url,
            'description': f'{action} using the API',
            'params': '',
            'example_body': body,
        })

    return endpoints


def parse_doc_page(markdown: str, page_url: str) -> list:
    """
    Parse a single doc page into endpoint dicts.
    Auto-detects whether it's an API reference page or a tutorial.
    """
    if '## Method:' in markdown:
        return extract_endpoints_from_reference(markdown)
    else:
        return extract_endpoints_from_tutorial(markdown)


# ── Groq enrichment (optional, for better descriptions) ──────────────

def enrich_with_groq(endpoints: list, api_name: str) -> list:
    """
    Use Groq to add better action descriptions to extracted endpoints.
    This is a single, focused call — not parsing raw docs.
    """
    if not GROQ_API_KEY or not endpoints:
        return endpoints

    summary = []
    for ep in endpoints:
        summary.append(f"- {ep['method']} {ep['endpoint']} (action: {ep['action']})")

    prompt = f"""Given these {api_name} API endpoints, return a JSON array with a short action phrase and 1-sentence description for each.

Endpoints:
{chr(10).join(summary)}

Return ONLY a JSON array like:
[
  {{"endpoint": "...", "action": "generate text content", "description": "Generate text from a prompt using the Gemini model."}},
  ...
]

Rules:
- action should be 2-4 words, verb phrase, lowercase
- description should be 1 sentence, practical
- Return one entry per endpoint, same order
- Return ONLY valid JSON. No explanation."""

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
                'max_tokens': 2000
            },
            timeout=30.0
        )
        if resp.status_code != 200:
            return endpoints  # silently fall back to regex-derived descriptions

        content = resp.json()['choices'][0]['message']['content'].strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content[:-3]
            content = content.strip()

        enriched = json.loads(content)
        if isinstance(enriched, list) and len(enriched) == len(endpoints):
            for i, ep in enumerate(endpoints):
                if enriched[i].get('action'):
                    ep['action'] = enriched[i]['action']
                if enriched[i].get('description'):
                    ep['description'] = enriched[i]['description']
    except Exception:
        pass  # enrichment is optional, fall back to regex descriptions

    return endpoints


# ── Main pipeline ─────────────────────────────────────────────────────

def scrape_api_docs(api: str) -> dict:
    """Full pipeline: get raw markdown → extract endpoints → enrich → save docs.json."""
    source = API_SOURCES.get(api)
    if not source:
        return {}

    print(f"\n  {C.BOLD}📡 First time using {source['name']} — building docs...{C.RESET}")
    print(f"  {C.DIM}This only happens once. Results are saved for next time.{C.RESET}\n")

    # Step 1: Get raw markdown per URL (uses raw.json cache)
    raw_by_url = fetch_raw_markdown(api)
    if not raw_by_url:
        return {}

    # Step 2: Extract endpoints from each page using regex
    all_endpoints = []
    for url, md in raw_by_url.items():
        if not md:
            continue
        print(f"  {C.DIM}Parsing: {url}...{C.RESET}")
        endpoints = parse_doc_page(md, url)
        if endpoints:
            print(f"  {C.GREEN}✓ {len(endpoints)} endpoints{C.RESET}")
            all_endpoints.extend(endpoints)
        else:
            print(f"  {C.DIM}  (no endpoints found){C.RESET}")

    if not all_endpoints:
        print(f"  {C.RED}✗ Could not extract any endpoints{C.RESET}")
        return {}

    # Step 3: Filter out template URLs and deduplicate
    concrete = [ep for ep in all_endpoints if '{' not in ep.get('endpoint', '')]
    seen_actions = {}
    unique = []
    for ep in concrete:
        url = ep.get('endpoint', '')
        if not url:
            continue

        # Deduplicate by the action suffix (e.g. :generateContent)
        # For URLs like /v1beta/models (no colon action), use last path segment
        if ':' in url.split('/')[-1]:
            action_key = url.split(':')[-1]
        else:
            action_key = url.rstrip('/').rsplit('/', 1)[-1]

        if action_key in seen_actions:
            existing = seen_actions[action_key]
            # Prefer: has example_body > no example_body
            # Prefer: stable model name > preview model name
            is_better = False
            if not existing.get('example_body') and ep.get('example_body'):
                is_better = True
            elif 'preview' in existing['endpoint'] and 'preview' not in ep['endpoint']:
                is_better = True
            if is_better:
                unique[unique.index(existing)] = ep
                seen_actions[action_key] = ep
            continue

        seen_actions[action_key] = ep
        unique.append(ep)

    # Step 4: Enrich with Groq for better descriptions (single LLM call)
    print(f"  {C.DIM}Enriching descriptions...{C.RESET}")
    unique = enrich_with_groq(unique, source['name'])

    # Step 5: Save
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
    print(f"\n  {C.GREEN}✓ Saved {len(unique)} endpoints to docs.json{C.RESET}\n")
    return parsed


def load_api_docs(api: str) -> dict:
    """
    Load docs for an API:
      1. docs.json exists with endpoints → return instantly
      2. Otherwise → scrape + parse + save
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
