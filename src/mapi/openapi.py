"""
openapi.py — Convert an OpenAPI 3 / Swagger spec into mapi's docs.json format.

Lots of APIs (and Mintlify-hosted docs) publish a machine-readable OpenAPI
spec — a JSON or YAML file with every path, parameter, request/response schema,
and security scheme. That's the ideal grounding source: we convert it straight
into the same enriched docs.json shape the Postman converter produces
(query_params + example_body + example_response + per-endpoint auth), so the
code generator works from the real spec instead of guessing.

Flow:
  openapi_url (in config) → fetch spec (json|yaml) → convert_openapi_to_docs() → docs.json
"""
import json

import httpx

from .config import DATA_DIR, API_SOURCES
from .display import C

MAX_DESC_CHARS = 4000


def _load_spec_text(text: str) -> dict:
    """Parse a spec that may be JSON or YAML."""
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    import yaml  # optional dep; only needed for YAML specs
    return yaml.safe_load(text)


def _deref(node, spec, seen=None):
    """Resolve a local $ref (guards against cycles)."""
    seen = seen or set()
    while isinstance(node, dict) and '$ref' in node:
        ref = node['$ref']
        if ref in seen:
            return {}
        seen = seen | {ref}
        cur = spec
        for part in ref.lstrip('#/').split('/'):
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        node = cur
    return node


def _string_example(schema: dict):
    fmt = schema.get('format', '')
    desc = (schema.get('description') or '').lower()
    name = (schema.get('_name') or '').lower()
    if fmt == 'date-time':
        return '2024-01-01T00:00:00Z'
    if fmt == 'date':
        return '2024-01-01'
    if fmt == 'uuid':
        return '00000000-0000-0000-0000-000000000000'
    if fmt == 'email' or 'email' in desc or 'email' in name:
        return 'user@example.com'
    if 'password' in name:
        return 'YOUR_PASSWORD'
    if 'phone' in name:
        return '+15555550123'
    return 'string'


def _example_from_schema(schema, spec, depth=0):
    """Synthesize a representative example value from a JSON schema."""
    schema = _deref(schema, spec)
    if not isinstance(schema, dict):
        return None
    if 'example' in schema:
        return schema['example']
    if 'default' in schema:
        return schema['default']
    if schema.get('enum'):
        return schema['enum'][0]
    for key in ('allOf', 'oneOf', 'anyOf'):
        if schema.get(key):
            return _example_from_schema(schema[key][0], spec, depth + 1)
    t = schema.get('type')
    if t == 'object' or 'properties' in schema:
        if depth > 6:
            return {}
        out = {}
        for name, prop in (schema.get('properties') or {}).items():
            prop = _deref(prop, spec)
            if isinstance(prop, dict):
                prop = {**prop, '_name': name}
            out[name] = _example_from_schema(prop, spec, depth + 1)
        return out
    if t == 'array':
        if depth > 6:
            return []
        return [_example_from_schema(schema.get('items', {}), spec, depth + 1)]
    if t in ('integer', 'number'):
        return 0
    if t == 'boolean':
        return False
    return _string_example(schema)


def _scheme_auth(name: str, schemes: dict, fallback: str) -> str:
    """Human-readable auth instruction for a named security scheme."""
    s = schemes.get(name, {})
    if s.get('type') == 'http' and s.get('scheme') == 'bearer':
        return 'Pass token as header: Authorization: Bearer YOUR_API_KEY'
    if s.get('type') == 'apiKey' and s.get('in') == 'header':
        return f"Pass token as header: {s.get('name', 'Authorization')}: YOUR_API_KEY"
    if s.get('type') == 'apiKey' and s.get('in') == 'query':
        return f"Pass token as query parameter: ?{s.get('name', 'key')}=YOUR_API_KEY"
    return fallback


def _content_json(container: dict, spec: dict):
    """Pull an application/json example (explicit or schema-derived) from a body/response."""
    content = (container or {}).get('content', {}) or {}
    media = content.get('application/json') or next(
        (v for k, v in content.items() if 'json' in k.lower()), {})
    if not isinstance(media, dict):
        return None
    if 'example' in media:
        return media['example']
    examples = media.get('examples')
    if isinstance(examples, dict) and examples:
        first = _deref(next(iter(examples.values())), spec)
        if isinstance(first, dict) and 'value' in first:
            return first['value']
    if media.get('schema') is not None:
        return _example_from_schema(media['schema'], spec)
    return None


def _base_of(servers) -> str:
    """First server URL from a `servers` list, trailing slash trimmed."""
    if servers and isinstance(servers[0], dict):
        return (servers[0].get('url') or '').rstrip('/')
    return ''


def convert_openapi_to_docs(spec: dict, *, service_name: str, auth: str,
                            doc_url: str) -> dict:
    """Convert an OpenAPI 3 / Swagger dict into mapi docs.json shape."""
    base = _base_of(spec.get('servers') or [])
    schemes = (spec.get('components', {}) or {}).get('securitySchemes', {}) or {}
    global_sec = spec.get('security')
    methods = ('get', 'post', 'put', 'patch', 'delete')
    endpoints = []

    for path, ops in (spec.get('paths') or {}).items():
        if not isinstance(ops, dict):
            continue
        shared = ops.get('parameters', []) if isinstance(ops.get('parameters'), list) else []
        # OpenAPI lets a path item (and operation) override the root server.
        path_base = _base_of(ops.get('servers') or [])
        for method, op in ops.items():
            if method not in methods or not isinstance(op, dict):
                continue

            # Precedence: operation-level > path-level > root server.
            op_base = _base_of(op.get('servers') or []) or path_base or base
            url = f"{op_base}{path}"
            summary = (op.get('summary') or '').strip()
            action = summary.lower() if summary else f"{method} {path.strip('/').split('/')[-1] or path}"
            description = (op.get('description') or summary or '').strip()[:MAX_DESC_CHARS]

            path_parts, query_params = [], []
            for p in list(op.get('parameters') or []) + list(shared):
                p = _deref(p, spec)
                if not isinstance(p, dict) or not p.get('name'):
                    continue
                if p.get('in') == 'path':
                    path_parts.append(f"{p['name']} (path, required)")
                elif p.get('in') == 'query':
                    query_params.append({
                        'name': p['name'],
                        'description': (p.get('description') or '')[:120],
                    })

            example_body = None
            rb = _deref(op.get('requestBody', {}), spec)
            if isinstance(rb, dict):
                example_body = _content_json(rb, spec)

            example_response = None
            responses = op.get('responses', {}) or {}
            for code in ('200', 200, '201', 201, '2XX'):
                if code in responses:
                    example_response = _content_json(_deref(responses[code], spec), spec)
                    if example_response is not None:
                        break

            # Per-endpoint auth (Drumroll mixes raw-JWT and Bearer).
            sec = op.get('security', global_sec)
            ep_auth = auth
            if isinstance(sec, list):
                if not sec:
                    ep_auth = 'No authentication required'
                elif isinstance(sec[0], dict) and sec[0]:
                    ep_auth = _scheme_auth(next(iter(sec[0])), schemes, auth)

            body_keys = list(example_body.keys())[:12] if isinstance(example_body, dict) else []
            params_str = ', '.join(
                path_parts
                + [f"{q['name']} (query)" for q in query_params]
                + [f"{k} (body)" for k in body_keys]
            )

            endpoints.append({
                'action': action,
                'method': method.upper(),
                'endpoint': url,
                'description': description or f"{method.upper()} {path}",
                'params': params_str,
                'query_params': query_params,
                'tags': op.get('tags') or [],
                'auth': ep_auth,
                'example_body': example_body if method in ('post', 'put', 'patch') else None,
                'example_response': example_response,
            })

    return {
        'service_name': service_name,
        'auth': auth,
        'doc_url': doc_url,
        'base_url': base,
        'endpoints': endpoints,
    }


def load_docs_from_openapi(api: str) -> dict:
    """Fetch an OpenAPI spec for `api` and convert it to docs.json."""
    source = API_SOURCES.get(api)
    if not source or not source.get('openapi_url'):
        return {}

    url = source['openapi_url']
    print(f"\n  {C.BOLD}📗 Building {source['name']} docs from OpenAPI spec...{C.RESET}")
    print(f"  {C.DIM}This only happens once. Results are saved for next time.{C.RESET}\n")
    print(f"  {C.DIM}Fetching: {url}{C.RESET}")

    try:
        resp = httpx.get(url, timeout=60.0, follow_redirects=True)
        if resp.status_code != 200:
            print(f"  {C.RED}✗ Spec fetch returned {resp.status_code}{C.RESET}")
            return {}
        spec = _load_spec_text(resp.text)
    except ImportError:
        print(f"  {C.RED}✗ This spec is YAML; install pyyaml (pip install pyyaml){C.RESET}")
        return {}
    except Exception as e:
        print(f"  {C.RED}✗ Failed to fetch/parse spec: {e}{C.RESET}")
        return {}

    docs = convert_openapi_to_docs(
        spec,
        service_name=source['name'],
        auth=source.get('auth', 'See documentation'),
        doc_url=source.get('doc_url', url),
    )

    if not docs.get('endpoints'):
        print(f"  {C.RED}✗ No endpoints found in spec{C.RESET}")
        return {}

    out_dir = DATA_DIR / api
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'docs.json', 'w') as f:
        json.dump(docs, f, indent=2)
    print(f"  {C.GREEN}✓ Saved {len(docs['endpoints'])} endpoints{C.RESET}\n")
    return docs
