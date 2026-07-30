"""
postman.py — Convert a Postman Collection (v2) into mapi's docs.json format.

Many APIs publish their docs as a Postman collection (getpostman.com documenter)
instead of a plain HTML page or an OpenAPI spec. The published documenter URL
exposes the underlying collection JSON, which is far richer and more reliable to
parse than the JS-rendered HTML page — it already contains every request's
method, URL, headers, and example body.

Flow:
  postman_url (in config) → fetch collection JSON → convert_collection() → docs.json
"""
import json
import re
from html import unescape

import httpx

from .config import DATA_DIR, API_SOURCES
from .display import C

MAX_DESC_CHARS = 4000


def _lenient_json(raw: str):
    """Parse a JSON-ish string that may contain trailing commas or // comments.

    Postman example bodies are hand-written and frequently include trailing
    commas or comments that strict json.loads rejects. We try strict first,
    then a cleaned-up pass, and finally give up and return the raw string so
    the code generator can still use it verbatim.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    cleaned = re.sub(r'//[^\n]*', '', raw)          # strip line comments
    cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)  # block comments
    cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)  # trailing commas
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return raw  # keep the raw text as a fallback body


def _html_to_text(html) -> str:
    """Convert a Postman HTML description into clean, readable text.

    Postman descriptions are HTML and carry the good stuff — the full parameter
    list, filter-syntax examples, and example responses. The old converter
    stripped tags and truncated to 300 chars, which cut off exactly the
    "Request Parameters" section. This keeps the structure (bullets + fenced
    code blocks) so the model sees real params and filter examples.
    """
    if isinstance(html, dict):
        html = html.get('content', '')
    if not html:
        return ''
    s = html
    # <pre><code>…</code></pre> → fenced code block
    s = re.sub(r'<pre[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>',
               lambda m: '\n```\n' + unescape(m.group(1)).strip() + '\n```\n',
               s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'<li[^>]*>(.*?)</li>',
               lambda m: '- ' + m.group(1).strip() + '\n', s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'</p\s*>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<hr\s*/?>', '\n', s, flags=re.IGNORECASE)
    s = re.sub(r'<[^>]+>', '', s)          # strip any remaining tags
    s = unescape(s)
    s = re.sub(r'\n{3,}', '\n\n', s)       # collapse blank runs
    return s.strip()


def _parse_query_params(text: str) -> list:
    """Pull the documented query params out of a readable description.

    JobNimbus descriptions are inconsistent, so we tolerate several shapes:
      "size - number of elements to return (default: 1000)"
      "sort_direction = which direction to sort"
      "- from: Starting index for pagination (e.g., 10)"
    We isolate the parameters section (headed "Request Parameters" or
    "Query Parameters", up to the "Response" section) so we don't scrape keys
    out of the example JSON body that follows.
    """
    m = re.search(r'(?:Request|Query)\s+Parameters(.*?)(?:\n\s*Response\b|$)',
                  text, re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    params = []
    seen = set()
    for line in m.group(1).splitlines():
        line = re.sub(r'^\s*[-*]\s+', '', line.strip())   # drop bullet marker
        # name, an optional "(optional)"-style annotation, then - = or :
        pm = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\([^)]*\))?\s*[-=:]\s+(.+)', line)
        if not pm:
            continue
        name = pm.group(1)
        if name.lower() in seen or len(name) < 2:
            continue
        seen.add(name.lower())
        params.append({'name': name, 'description': pm.group(2).strip()[:120]})
    return params


def _example_response(item: dict, raw_description) -> object:
    """Best example response for an endpoint.

    Prefers a saved response the collection ships (item.response[]); otherwise
    falls back to the "Response" example embedded in the HTML description.
    Returns a parsed dict when possible, else the raw string, else None.
    """
    for resp in (item.get('response') or []):
        body = resp.get('body')
        if body:
            return _lenient_json(body)
    # Fallback: the code block under the <strong>Response</strong> header.
    html = raw_description.get('content', '') if isinstance(raw_description, dict) else (raw_description or '')
    m = re.search(r'<strong>\s*Response\s*</strong>.*?<pre[^>]*>\s*<code[^>]*>(.*?)</code>',
                  html, re.DOTALL | re.IGNORECASE)
    if m:
        return _lenient_json(unescape(m.group(1)).strip())
    return None


def _raw_url(url) -> str:
    """Extract a plain URL string from a Postman url field (string or object)."""
    if isinstance(url, str):
        return url
    if isinstance(url, dict):
        raw = url.get('raw')
        if isinstance(raw, str) and raw:
            return raw
        host = url.get('host')
        if isinstance(host, list):
            host = '.'.join(host)
        path = url.get('path')
        if isinstance(path, list):
            path = '/'.join(str(p) for p in path)
        return f"{host or ''}/{path or ''}".rstrip('/')
    return ''


def _params(url, body, query_params=None) -> str:
    """Build a human-readable params string from path vars, query, and body keys."""
    parts = []
    raw = _raw_url(url)
    # path placeholders: <jnid>, :id, {id}
    for token in re.findall(r'<([^>]+)>|:(\w+)|\{(\w+)\}', raw):
        name = next((t for t in token if t), None)
        if name:
            parts.append(f"{name} (path, required)")
    # query params parsed from the description (size, from, filter, …)
    for qp in (query_params or []):
        parts.append(f"{qp['name']} (query)")
    # query params attached structurally to the Postman url
    if isinstance(url, dict):
        for q in url.get('query') or []:
            if isinstance(q, dict) and q.get('key'):
                req = '' if q.get('disabled') else ' (query)'
                parts.append(f"{q['key']}{req}")
    # body keys
    if isinstance(body, dict):
        for k in list(body.keys())[:12]:
            parts.append(f"{k} (body)")
    return ', '.join(dict.fromkeys(parts))  # dedupe, keep order


def convert_collection(collection: dict, *, service_name: str, auth: str,
                       doc_url: str) -> dict:
    """Convert a Postman Collection v2 dict into mapi docs.json shape."""
    endpoints = []
    seen = set()

    def walk(items, folder=''):
        for it in items:
            if 'item' in it:  # a folder
                walk(it['item'], it.get('name', folder))
                continue
            req = it.get('request')
            if not isinstance(req, dict):
                continue
            method = (req.get('method') or 'GET').upper()
            raw_url = _raw_url(req.get('url'))
            # Skip prose entries like "The Presigned URL from ..." — not real URLs.
            if not raw_url.startswith('http'):
                continue

            name = (it.get('name') or '').strip()
            action = name.lower() if name else f"{method.lower()} {raw_url.rsplit('/', 1)[-1]}"

            key = (method, raw_url, action)
            if key in seen:
                continue
            seen.add(key)

            raw_description = it.get('description') or req.get('description') or ''
            full_description = _html_to_text(raw_description) or f"{method} {name}"

            body_raw = (req.get('body') or {}).get('raw')
            example_body = _lenient_json(body_raw) if body_raw else None

            # Real query params documented in the description (size, from,
            # filter, sort_field, …) — the thing the model was inventing before.
            # Parse from the FULL text; some params sit past the display cap.
            query_params = _parse_query_params(full_description)
            example_response = _example_response(it, raw_description)
            description = full_description[:MAX_DESC_CHARS]

            endpoints.append({
                'action': action,
                'method': method,
                'endpoint': raw_url,
                'description': description,
                'params': _params(req.get('url'), example_body, query_params),
                'query_params': query_params,
                'tags': [folder] if folder else [],
                'example_body': example_body if method in ('POST', 'PUT', 'PATCH') else None,
                'example_response': example_response,
            })

    walk(collection.get('item', []))

    return {
        'service_name': service_name,
        'auth': auth,
        'doc_url': doc_url,
        'endpoints': endpoints,
    }


def load_docs_from_postman(api: str) -> dict:
    """Fetch a Postman collection for `api` and convert it to docs.json."""
    source = API_SOURCES.get(api)
    if not source or not source.get('postman_url'):
        return {}

    url = source['postman_url']
    print(f"\n  {C.BOLD}📮 Building {source['name']} docs from Postman collection...{C.RESET}")
    print(f"  {C.DIM}This only happens once. Results are saved for next time.{C.RESET}\n")
    print(f"  {C.DIM}Fetching: {url}{C.RESET}")

    try:
        resp = httpx.get(url, timeout=60.0, follow_redirects=True)
        if resp.status_code != 200:
            print(f"  {C.RED}✗ Postman returned {resp.status_code}{C.RESET}")
            return {}
        collection = resp.json()
    except Exception as e:
        print(f"  {C.RED}✗ Failed to fetch/parse collection: {e}{C.RESET}")
        return {}

    docs = convert_collection(
        collection,
        service_name=source['name'],
        auth=source.get('auth', 'See documentation'),
        doc_url=source.get('doc_url', url),
    )

    if not docs.get('endpoints'):
        print(f"  {C.RED}✗ No endpoints found in collection{C.RESET}")
        return {}

    out_dir = DATA_DIR / api
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'docs.json', 'w') as f:
        json.dump(docs, f, indent=2)
    print(f"  {C.GREEN}✓ Saved {len(docs['endpoints'])} endpoints{C.RESET}\n")
    return docs
