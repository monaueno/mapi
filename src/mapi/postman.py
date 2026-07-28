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

import httpx

from .config import DATA_DIR, API_SOURCES
from .display import C


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


def _params(url, body) -> str:
    """Build a human-readable params string from path vars, query, and body keys."""
    parts = []
    raw = _raw_url(url)
    # path placeholders: <jnid>, :id, {id}
    for token in re.findall(r'<([^>]+)>|:(\w+)|\{(\w+)\}', raw):
        name = next((t for t in token if t), None)
        if name:
            parts.append(f"{name} (path, required)")
    # query params
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

            description = it.get('description') or req.get('description') or ''
            if isinstance(description, dict):
                description = description.get('content', '')
            description = re.sub(r'<[^>]+>', '', str(description)).strip()

            body_raw = (req.get('body') or {}).get('raw')
            example_body = _lenient_json(body_raw) if body_raw else None

            endpoints.append({
                'action': action,
                'method': method,
                'endpoint': raw_url,
                'description': (description[:300] or f"{method} {name}").strip(),
                'params': _params(req.get('url'), example_body),
                'tags': [folder] if folder else [],
                'example_body': example_body if method in ('POST', 'PUT', 'PATCH') else None,
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
