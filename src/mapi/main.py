"""
main.py — Entry point. Parses args and orchestrates the flow.

Flow:
    1. Parse command:  mapi -python gemini generate text
    2. Check last_result.json → cache hit? print and done
    3. Load docs    →  not scraped yet? scraper.py handles it (Firecrawl)
    4. Generate     →  Groq creates the code
    5. Verify       →  hit the real endpoint to confirm it exists
    6. Save         →  write last_result.json (cache + test source)
    7. Display      →  print the code with verification badge
"""
import json
import sys

import httpx

from .config import GROQ_API_KEY, API_SOURCES, DATA_DIR, LAST_RESULT_PATH
from .scraper import load_api_docs
from .generator import ask_groq
from .verifier import verify_endpoint
from .display import C, print_usage, print_result

HTTP_STATUS_TEXT = {
    200: 'OK', 201: 'Created', 204: 'No Content',
    400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden',
    404: 'Not Found', 405: 'Method Not Allowed', 429: 'Too Many Requests',
    500: 'Internal Server Error', 502: 'Bad Gateway', 503: 'Service Unavailable',
}


def parse_args(args):
    """Parse CLI arguments into {language, api, query}."""
    if not args:
        return {'error': 'usage'}

    # mapi test api_key="..."
    if args[0] == 'test':
        for arg in args[1:]:
            if arg.startswith('api_key='):
                return {'command': 'test', 'api_key': arg.split('=', 1)[1].strip('"').strip("'")}
        return {'error': 'test_usage'}

    # mapi help
    if args[0] in ('help', '--help', '-h'):
        return {'error': 'usage'}

    # mapi -python gemini generate text
    if not args[0].startswith('-'):
        return {'error': 'usage'}

    language = args[0][1:]  # strip the dash
    if len(args) < 3:
        return {'error': 'usage'}

    api = args[1].lower()
    query = ' '.join(args[2:])
    return {'language': language, 'api': api, 'query': query}


def build_request_from_docs(api: str, method: str, endpoint_url: str, query: str) -> dict:
    """Build a test request from the API source config and scraped docs.

    Uses the auth method from config.py and the example_body from the matched
    endpoint in docs.json — works regardless of what language was generated.
    """
    source = API_SOURCES.get(api, {})
    auth_str = source.get('auth', '')

    headers = {'Content-Type': 'application/json'}
    # Parse auth config into a header
    # Format: "Pass API key as header: x-goog-api-key: YOUR_API_KEY"
    if 'header:' in auth_str.lower():
        # Extract "x-goog-api-key: YOUR_API_KEY" from the auth string
        after_header = auth_str.split('header:', 1)[1].strip()
        if ':' in after_header:
            key, val = after_header.split(':', 1)
            headers[key.strip()] = val.strip()

    # Get the example_body from the matched endpoint in docs
    body = None
    docs = load_api_docs(api)
    if docs:
        for ep in docs.get('endpoints', []):
            if ep['endpoint'] == endpoint_url:
                body = ep.get('example_body')
                break

    # Substitute the user's query into the body text field
    if body and isinstance(body, dict):
        body = _sub_query_in_body(body, query)

    return {
        'method': method,
        'url': endpoint_url,
        'headers': headers,
        'body': body,
    }


def _sub_query_in_body(obj, query):
    """Replace example text in the body with the user's actual query."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return [_sub_query_in_body(item, query) for item in obj]
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == 'text' and isinstance(v, str):
                result[k] = query
            else:
                result[k] = _sub_query_in_body(v, query)
        return result
    return obj


def save_last_result(cache_key, language, code, endpoint, verification, api, query):
    """Save the result — serves as both cache and source for `mapi test`."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LAST_RESULT_PATH, 'w') as f:
        json.dump({
            'cache_key': cache_key,
            'language': language,
            'code': code,
            'endpoint': endpoint,
            'verification': verification,
            'api': api,
            'query': query,
        }, f, indent=2)


def load_last_result():
    """Load the last saved result, or None."""
    if LAST_RESULT_PATH.exists():
        with open(LAST_RESULT_PATH) as f:
            return json.load(f)
    return None


def sub_api_key(obj, api_key):
    """Recursively substitute YOUR_API_KEY in strings, dicts, and lists."""
    if isinstance(obj, str):
        return obj.replace('YOUR_API_KEY', api_key).replace('YOUR_ACCESS_TOKEN', api_key)
    if isinstance(obj, dict):
        return {k: sub_api_key(v, api_key) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sub_api_key(v, api_key) for v in obj]
    return obj


def run_test(api_key):
    """Test the last generated request with a real API key. Prints only the status line."""
    entry = load_last_result()
    if not entry:
        print(f"  {C.RED}No previous result to test. Run a query first.{C.RESET}")
        return

    endpoint_str = entry.get('endpoint', '')
    api = entry.get('api', '')
    query = entry.get('query', '')

    if not endpoint_str or endpoint_str == 'unknown':
        print(f"  {C.RED}No endpoint saved. Re-run your query to regenerate.{C.RESET}")
        return

    # Parse "POST https://..." into method + url
    parts = endpoint_str.split(' ', 1)
    method = parts[0] if len(parts) == 2 else 'GET'
    endpoint_url = parts[1] if len(parts) == 2 else parts[0]

    # Build request from docs + config (not from generated code)
    req = build_request_from_docs(api, method, endpoint_url, query)

    # Substitute the user's real API key
    headers = sub_api_key(req['headers'], api_key)
    url = sub_api_key(req['url'], api_key)
    body = sub_api_key(req.get('body'), api_key)

    print(f"\n  {C.DIM}Testing: {method} {endpoint_url}{C.RESET}")

    try:
        if method.upper() == 'POST':
            resp = httpx.post(url, headers=headers, json=body, timeout=30.0)
        elif method.upper() == 'GET':
            resp = httpx.get(url, headers=headers, timeout=30.0)
        else:
            resp = httpx.request(method.upper(), url, headers=headers, json=body, timeout=30.0)

        status = resp.status_code
        reason = HTTP_STATUS_TEXT.get(status, '')
        status_line = f"{status} {reason}" if reason else str(status)

        if 200 <= status < 300:
            print(f"  {C.GREEN}✓ {status_line}{C.RESET}\n")
        else:
            print(f"  {C.RED}✗ {status_line}{C.RESET}\n")
    except Exception as e:
        print(f"  {C.RED}✗ Request failed: {e}{C.RESET}\n")


def main():
    args = sys.argv[1:]
    parsed = parse_args(args)

    # ── Handle errors ───────────────────────────────────────────────
    if 'error' in parsed:
        if parsed['error'] == 'test_usage':
            print(f"\n  {C.RED}Usage: mapi test api_key=\"YOUR_KEY\"{C.RESET}\n")
        else:
            print_usage()
        return

    # ── Handle test command ─────────────────────────────────────────
    if parsed.get('command') == 'test':
        run_test(parsed['api_key'])
        return

    language = parsed['language']
    api = parsed['api']
    query = parsed['query']

    # ── Check Groq key ──────────────────────────────────────────────
    if not GROQ_API_KEY:
        print(f"\n  {C.RED}✗ GROQ_API_KEY not set{C.RESET}")
        print(f"  Get a free key: {C.CYAN}https://console.groq.com{C.RESET}")
        print(f"  Add to .env: GROQ_API_KEY=gsk_your_key\n")
        return

    # ── Check API exists ────────────────────────────────────────────
    if api not in API_SOURCES:
        print(f"\n  {C.RED}✗ Unknown API: '{api}'{C.RESET}")
        print(f"  Available: {C.CYAN}{', '.join(API_SOURCES.keys())}{C.RESET}\n")
        return

    # ── Step 1: Check cache (last_result.json) ──────────────────────
    cache_key = f"{language}:{api}:{query}"
    cached = load_last_result()
    if cached and cached.get('cache_key') == cache_key:
        print_result(cached['code'], cached.get('verification', {}), language, from_cache=True)
        return

    # ── Step 2: Load docs (auto-scrapes if first time) ──────────────
    docs = load_api_docs(api)
    if not docs or not docs.get('endpoints'):
        print(f"\n  {C.RED}✗ No docs available for '{api}'{C.RESET}")
        print(f"  Make sure FIRECRAWL_API_KEY is set in .env\n")
        return

    # ── Step 3: Generate code with Groq ─────────────────────────────
    print(f"\n  {C.DIM}Generating {language} code for: {api} → {query}...{C.RESET}")
    code = ask_groq(language, query, docs)

    if code.startswith('Error:'):
        print(f"\n  {C.RED}{code}{C.RESET}\n")
        return

    # ── Step 4: Find endpoint URL in generated code ─────────────────
    endpoint_url = ''
    method = 'GET'
    for ep in docs['endpoints']:
        if ep['endpoint'] in code:
            endpoint_url = ep['endpoint']
            method = ep['method']
            break

    # ── Step 5: Verify the endpoint is real ─────────────────────────
    print(f"  {C.DIM}Verifying endpoint...{C.RESET}")
    if endpoint_url:
        verification = verify_endpoint(method, endpoint_url)
    else:
        verification = {'verified': False, 'message': 'Could not extract endpoint URL'}

    # ── Step 6: Save last result ────────────────────────────────────
    endpoint_str = f"{method} {endpoint_url}" if endpoint_url else 'unknown'
    save_last_result(cache_key, language, code, endpoint_str, verification, api, query)

    # ── Step 7: Display ─────────────────────────────────────────────
    print_result(code, verification, language, from_cache=False)


if __name__ == '__main__':
    main()
