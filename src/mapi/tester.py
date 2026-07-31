"""
tester.py — Live end-to-end testing of a configured API.

Two commands:
  mapi login <api> email=… password=…   → authenticate, capture the token
  mapi testall <api> companyId=… …      → hit every endpoint, report status

The token is saved to data/{api}/auth.json (gitignored — it's a secret) so
credentials only ever live on this machine. `testall` builds each request from
the grounded docs.json (real auth, example_body, path params), fires it against
the live API, and prints the HTTP status per endpoint so you can see at a glance
which documented endpoints actually work.
"""
import json
import re
import time

import httpx

from .config import API_SOURCES, DATA_DIR
from .scraper import load_api_docs
from .display import C

WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')


# ── token store ─────────────────────────────────────────────────────
def _auth_path(api: str):
    d = DATA_DIR / api
    d.mkdir(parents=True, exist_ok=True)
    return d / 'auth.json'


def load_auth(api: str) -> dict:
    p = _auth_path(api)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_auth(api: str, data: dict):
    with open(_auth_path(api), 'w') as f:
        json.dump(data, f, indent=2)


# ── helpers ─────────────────────────────────────────────────────────
def _dig(obj, path: str):
    """Read a dotted path (e.g. 'data.token') out of a JSON response."""
    cur = obj
    for part in path.split('.'):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _fill(template, values: dict):
    """Recursively replace {placeholders} in a template with provided values."""
    if isinstance(template, str):
        def repl(m):
            return str(values.get(m.group(1), m.group(0)))
        return re.sub(r'\{(\w+)\}', repl, template)
    if isinstance(template, dict):
        return {k: _fill(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, values) for v in template]
    return template


def _lookup_var(name: str, variables: dict):
    """Find a var for a field name, tolerating the Id-suffix convention.

    Path params use the id form (employeeId, companyId) while request bodies
    use the bare noun (employee, company). So a field 'employee' should match a
    provided 'employeeId', and vice versa.
    """
    candidates = [name, name + 'Id']
    stripped = re.sub(r'Id$', '', name)
    if stripped != name:
        candidates.append(stripped)
    for c in candidates:
        v = variables.get(c)
        if v not in (None, ''):
            return v
    return None


def _sub_path(url: str, variables: dict):
    """Substitute {name}/<name>/:name path params; report any that are missing."""
    missing = []

    def repl(m):
        name = m.group(1) or m.group(2) or m.group(3)
        v = _lookup_var(name, variables)
        if v not in (None, ''):
            return str(v)
        missing.append(name)
        return m.group(0)

    filled = re.sub(r'\{(\w+)\}|<(\w+)>|:(\w+)\b', repl, url)
    return filled, missing


def _fill_body(body, variables):
    """Substitute provided vars into a request body by field name.

    Uses the same Id-suffix tolerance as paths, so passing employeeId=… fills
    the body's 'employee' field — write tests then use real ids instead of the
    spec's placeholder values (e.g. "employee": "string").
    """
    if isinstance(body, dict):
        out = {}
        for k, v in body.items():
            if isinstance(v, (dict, list)):
                out[k] = _fill_body(v, variables)
            else:
                sub = _lookup_var(k, variables)
                out[k] = sub if sub is not None else v
        return out
    if isinstance(body, list):
        return [_fill_body(x, variables) for x in body]
    return body


def _auth_headers(auth_str: str, token: str, is_write: bool) -> dict:
    """Build request headers from an endpoint's auth description + the token."""
    headers = {}
    if is_write:
        headers['Content-Type'] = 'application/json'
    if not token:
        return headers
    a = (auth_str or '').lower()
    if 'bearer' in a:
        headers['Authorization'] = f'Bearer {token}'
    elif 'header:' in a:
        after = auth_str.split('header:', 1)[1].strip()
        name = after.split(':', 1)[0].strip() or 'Authorization'
        headers[name] = token
    else:
        headers['Authorization'] = token
    return headers


def _status_line(status: int, note: str = '') -> str:
    tail = f"  {C.DIM}{note}{C.RESET}" if note else ''
    if status == 0:
        return f"{C.RED}ERR{C.RESET}{tail}"
    if 200 <= status < 300:
        return f"{C.GREEN}{status} ✓{C.RESET}{tail}"
    if status in (401, 403):
        return f"{C.YELLOW}{status} auth{C.RESET}{tail}"
    return f"{C.RED}{status} ✗{C.RESET}{tail}"


# ── login ───────────────────────────────────────────────────────────
def do_login(api: str, creds: dict):
    source = API_SOURCES.get(api, {})
    spec = source.get('login')
    if not spec:
        print(f"\n  {C.RED}✗ No login flow configured for '{api}'.{C.RESET}\n")
        return

    missing = [f for f in spec.get('credential_fields', []) if f not in creds]
    if missing:
        print(f"\n  {C.RED}✗ Missing: {', '.join(missing)}{C.RESET}")
        print(f"  Usage: {C.CYAN}mapi login {api} {' '.join(f'{f}=…' for f in spec.get('credential_fields', []))}{C.RESET}\n")
        return

    body = _fill(spec.get('body', {}), creds)
    method = spec.get('method', 'POST').upper()
    url = spec['endpoint']
    print(f"\n  {C.DIM}Logging in: {method} {url}{C.RESET}")

    try:
        resp = httpx.request(method, url, json=body,
                             headers={'Content-Type': 'application/json'}, timeout=30.0)
    except Exception as e:
        print(f"  {C.RED}✗ Request failed: {e}{C.RESET}\n")
        return

    if not (200 <= resp.status_code < 300):
        print(f"  {C.RED}✗ Login failed ({resp.status_code}){C.RESET}")
        print(f"  {C.DIM}{resp.text[:200]}{C.RESET}\n")
        return

    try:
        data = resp.json()
    except Exception:
        print(f"  {C.RED}✗ Login response was not JSON{C.RESET}\n")
        return

    token = _dig(data, spec.get('token_field', 'token'))
    if not token:
        print(f"  {C.RED}✗ No token at '{spec.get('token_field')}' in response{C.RESET}\n")
        return

    variables = {}
    for var, field in (spec.get('save_vars') or {}).items():
        val = _dig(data, field)
        if val is not None:
            variables[var] = val

    save_auth(api, {'token': token, 'vars': variables, 'obtained_at': int(time.time())})
    print(f"  {C.GREEN}✓ Logged in — token saved{C.RESET}")
    if variables:
        pairs = ', '.join(f"{k}={v}" for k, v in variables.items())
        print(f"  {C.DIM}Captured: {pairs}{C.RESET}")
    print(f"  {C.DIM}Now run: {C.CYAN}mapi testall {api}{C.RESET}\n")


# ── testall ─────────────────────────────────────────────────────────
def run_testall(api: str, cli_vars: dict, include_writes: bool):
    source = API_SOURCES.get(api, {})
    docs = load_api_docs(api)
    if not docs or not docs.get('endpoints'):
        print(f"\n  {C.RED}✗ No docs for '{api}'.{C.RESET}\n")
        return

    auth = load_auth(api)
    token = auth.get('token')
    variables = {**auth.get('vars', {}), **cli_vars}
    login_endpoint = (source.get('login') or {}).get('endpoint')
    base_url = docs.get('base_url', '')

    if not token:
        print(f"\n  {C.YELLOW}⚠ Not logged in. Run: {C.CYAN}mapi login {api} …{C.RESET}")
        print(f"  {C.DIM}Testing anyway (auth'd endpoints will show 401).{C.RESET}")

    print(f"\n  {C.BOLD}Testing {docs['service_name']} — {len(docs['endpoints'])} endpoints{C.RESET}")
    if variables:
        print(f"  {C.DIM}vars: {', '.join(f'{k}={v}' for k, v in variables.items())}{C.RESET}")
    print()

    passed = failed = skipped = 0
    for ep in docs['endpoints']:
        method = ep.get('method', 'GET').upper()
        raw_url = ep.get('endpoint', '')
        label = (raw_url.replace(base_url, '') if base_url else raw_url) or raw_url
        tag = f"  {method:5} {label[:48]:48}"
        is_write = method in WRITE_METHODS

        # The login endpoint is validated by `mapi login`; don't re-fire it.
        if login_endpoint and raw_url == login_endpoint:
            if token:
                print(f"{tag} {C.GREEN}200 ✓{C.RESET}  {C.DIM}validated via `mapi login`{C.RESET}")
                passed += 1
            else:
                print(f"{tag} {C.DIM}skip  run `mapi login` to test{C.RESET}")
                skipped += 1
            continue

        url, missing = _sub_path(raw_url, variables)
        if missing:
            print(f"{tag} {C.DIM}skip{C.RESET}  {C.DIM}missing {', '.join(missing)} (pass {missing[0]}=…){C.RESET}")
            skipped += 1
            continue

        if is_write and not include_writes:
            print(f"{tag} {C.DIM}skip{C.RESET}  {C.DIM}write — add writes=yes to include{C.RESET}")
            skipped += 1
            continue

        headers = _auth_headers(ep.get('auth', docs.get('auth', '')), token, is_write)
        body = _fill_body(ep.get('example_body'), variables) if is_write else None

        try:
            resp = httpx.request(method, url, headers=headers,
                                 json=body if body is not None else None, timeout=20.0)
            status = resp.status_code
        except Exception as e:
            print(f"{tag} {_status_line(0, str(e)[:40])}")
            failed += 1
            continue

        print(f"{tag} {_status_line(status)}")
        if 200 <= status < 300:
            passed += 1
        else:
            failed += 1

    print(f"\n  {C.BOLD}Summary:{C.RESET} "
          f"{C.GREEN}{passed} passed{C.RESET}, "
          f"{C.RED}{failed} failed{C.RESET}, "
          f"{C.DIM}{skipped} skipped{C.RESET}\n")
