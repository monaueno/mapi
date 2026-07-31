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
import sys
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


def _last_test_path(api: str):
    return DATA_DIR / api / 'last_test.json'


def load_last_test(api: str) -> dict:
    p = _last_test_path(api)
    if p.exists():
        try:
            with open(p) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_last_test(api: str, variables: dict):
    keep = {k: v for k, v in variables.items() if v not in (None, '')}
    with open(_last_test_path(api), 'w') as f:
        json.dump(keep, f, indent=2)


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


def _prompt(label: str, default=None):
    """Ask for a value. Enter keeps the default (shown in [brackets]) or, when
    there is no default, returns None so the caller can skip."""
    suffix = f" [{default}]" if default not in (None, '') else ""
    try:
        raw = input(f"      {label}{suffix}: ").strip()
    except EOFError:
        return default
    return default if raw == '' else raw


def _confirm(question: str, default: bool = False) -> bool:
    try:
        raw = input(f"  {question} ").strip().lower()
    except EOFError:
        return default
    if raw == '':
        return default
    return raw in ('y', 'yes')


def _prompt_body(body, variables):
    """Interactively fill a write body: each field pre-filled with the known var
    or the spec's example as the default; Enter keeps it. Typed overrides are
    remembered in `variables` so they show up in next run's editable list."""
    filled = _fill_body(body, variables)
    if not isinstance(filled, dict):
        return filled
    print(f"      {C.DIM}(Enter keeps the shown default; type to override){C.RESET}")
    out = {}
    for k, v in filled.items():
        if isinstance(v, (dict, list)):
            out[k] = v  # nested values already had vars substituted
            continue
        entered = _prompt(k, default=v)
        out[k] = entered
        if entered not in (None, '') and entered != v:
            variables[k] = entered
    return out


def _review_saved_fields(saved: dict) -> dict:
    """Show saved fields as an editable list; Enter keeps each, type to change,
    a lone '-' drops the field. Returns the edited set."""
    print(f"\n  {C.BOLD}Last saved fields{C.RESET} {C.DIM}(Enter keeps, type to change, '-' removes){C.RESET}")
    edited = {}
    for k, v in saved.items():
        nv = _prompt(k, default=v)
        if nv not in (None, '', '-'):
            edited[k] = nv
    return edited


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
def run_testall(api: str, cli_vars: dict, include_writes: bool,
                interactive=None, use_last=False):
    source = API_SOURCES.get(api, {})
    docs = load_api_docs(api)
    if not docs or not docs.get('endpoints'):
        print(f"\n  {C.RED}✗ No docs for '{api}'.{C.RESET}\n")
        return

    auth = load_auth(api)
    token = auth.get('token')
    login_endpoint = (source.get('login') or {}).get('endpoint')
    base_url = docs.get('base_url', '')
    # Prompt for missing values only when we actually have a terminal.
    if interactive is None:
        interactive = sys.stdin.isatty()

    # Assemble the working vars. Precedence (low → high):
    #   last saved test  →  current login  →  explicit CLI args.
    variables = {}
    saved = load_last_test(api)
    if saved:
        if interactive:
            if _confirm(f"Use last saved fields? [Y/n]", default=True):
                variables.update(_review_saved_fields(saved))
        elif use_last:
            variables.update(saved)
    variables.update(auth.get('vars', {}))
    variables.update(cli_vars)

    if not token:
        print(f"\n  {C.YELLOW}⚠ Not logged in. Run: {C.CYAN}mapi login {api} …{C.RESET}")
        print(f"  {C.DIM}Testing anyway (auth'd endpoints will show 401).{C.RESET}")

    print(f"\n  {C.BOLD}Testing {docs['service_name']} — {len(docs['endpoints'])} endpoints{C.RESET}")
    if variables:
        print(f"  {C.DIM}vars: {', '.join(f'{k}={v}' for k, v in variables.items())}{C.RESET}")
    if interactive:
        print(f"  {C.DIM}Interactive: enter values when asked; blank skips that endpoint.{C.RESET}")
    print()

    passed = failed = skipped = 0
    for ep in docs['endpoints']:
        method = ep.get('method', 'GET').upper()
        raw_url = ep.get('endpoint', '')
        label = (raw_url.replace(base_url, '') if base_url else raw_url) or raw_url
        tag = f"  {method:5} {label[:48]:48}"
        is_write = method in WRITE_METHODS

        # Interactive: print the endpoint header once; prompts + result sit under
        # it. Non-interactive: header and result share one line (via report()).
        if interactive:
            print(tag)

        def report(result: str):
            print(f"      → {result}" if interactive else f"{tag} {result}")

        # The login endpoint is validated by `mapi login`; don't re-fire it.
        if login_endpoint and raw_url == login_endpoint:
            if token:
                report(f"{C.GREEN}200 ✓{C.RESET}  {C.DIM}validated via `mapi login`{C.RESET}")
                passed += 1
            else:
                report(f"{C.DIM}skip — run `mapi login` to test{C.RESET}")
                skipped += 1
            continue

        # Resolve path params, prompting for any we don't know yet.
        url, missing = _sub_path(raw_url, variables)
        if missing and interactive:
            for name in list(missing):
                val = _prompt(name)
                if val:
                    variables[name] = val
            url, missing = _sub_path(raw_url, variables)
        if missing:
            hint = '' if interactive else f" (pass {missing[0]}=…)"
            report(f"{C.DIM}skip — missing {', '.join(missing)}{hint}{C.RESET}")
            skipped += 1
            continue

        # Writes have side effects — gate them (confirm interactively, else opt-in).
        if is_write and not include_writes:
            if not (interactive and _confirm(f"{C.YELLOW}{method} is a write — run it? [y/N]{C.RESET}")):
                hint = '' if interactive else " (add writes=yes)"
                report(f"{C.DIM}skip — write{hint}{C.RESET}")
                skipped += 1
                continue

        headers = _auth_headers(ep.get('auth', docs.get('auth', '')), token, is_write)
        if is_write:
            body = _prompt_body(ep.get('example_body'), variables) if interactive else _fill_body(ep.get('example_body'), variables)
        else:
            body = None

        try:
            resp = httpx.request(method, url, headers=headers,
                                 json=body if body is not None else None, timeout=20.0)
            status = resp.status_code
        except Exception as e:
            report(_status_line(0, str(e)[:40]))
            failed += 1
            continue

        report(_status_line(status))
        if 200 <= status < 300:
            passed += 1
        else:
            failed += 1

    # Remember the fields for next time's "use last saved fields?" prompt.
    save_last_test(api, variables)

    print(f"\n  {C.BOLD}Summary:{C.RESET} "
          f"{C.GREEN}{passed} passed{C.RESET}, "
          f"{C.RED}{failed} failed{C.RESET}, "
          f"{C.DIM}{skipped} skipped{C.RESET}\n")
