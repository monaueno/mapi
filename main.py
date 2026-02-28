#!/usr/bin/env python3
"""
mapi — Find the exact API call you need with a simple command.

Usage:
    mapi -python gemini generate text
    mapi -curl googlemaps get directions
    mapi test api_key="YOUR_KEY"
"""
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ── Load .env ───────────────────────────────────────────────────────
load_dotenv()

# ── Config ──────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / 'data'
DOCS_PATH = DATA_DIR / 'google_docs.json'
CACHE_PATH = DATA_DIR / 'cache.json'
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = 'llama-3.3-70b-versatile'

# ── Colors ──────────────────────────────────────────────────────────
class C:
    BOLD    = '\033[1m'
    DIM     = '\033[2m'
    RESET   = '\033[0m'
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN    = '\033[96m'

# ── Load Data ───────────────────────────────────────────────────────
def load_docs() -> dict:
    with open(DOCS_PATH) as f:
        return json.load(f)

def load_cache() -> dict:
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}

def save_cache(cache: dict):
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)

# ── Verify Endpoint ────────────────────────────────────────────────
def verify_endpoint(method: str, url: str) -> dict:
    """
    Hit the endpoint with no real auth to see if it exists.
    401/403 = endpoint is real, just needs auth.
    404 = endpoint doesn't exist.
    """
    # Clean URL — remove query params for POST endpoints
    clean_url = url.split('?')[0] if method != 'GET' else url

    # For GET endpoints, add a dummy key so we hit the right path
    if method == 'GET' and '?' not in clean_url:
        clean_url += '?key=INVALID_KEY'
    elif method == 'GET' and 'key=' not in clean_url:
        clean_url += '&key=INVALID_KEY'

    try:
        if method == 'GET':
            resp = httpx.get(clean_url, timeout=10.0)
        elif method == 'POST':
            resp = httpx.post(
                clean_url,
                headers={'Content-Type': 'application/json'},
                json={},
                timeout=10.0
            )
        elif method == 'DELETE':
            resp = httpx.delete(clean_url, timeout=10.0)
        else:
            resp = httpx.request(method, clean_url, timeout=10.0)

        status = resp.status_code

        # Try to get error message from response
        try:
            body = resp.json()
            if 'error' in body:
                msg = body['error'].get('message', str(body['error']))
            else:
                msg = str(body)[:200]
        except Exception:
            msg = resp.text[:200]

        if status in (401, 403):
            return {'verified': True, 'status': status, 'message': msg}
        elif status == 404:
            return {'verified': False, 'status': 404, 'message': msg}
        elif status == 400:
            # 400 usually means endpoint exists but bad request body
            return {'verified': True, 'status': 400, 'message': msg}
        elif status == 200:
            return {'verified': True, 'status': 200, 'message': 'OK'}
        else:
            return {'verified': True, 'status': status, 'message': msg}

    except Exception as e:
        return {'verified': False, 'status': 0, 'message': str(e)}

# ── Groq LLM ───────────────────────────────────────────────────────
def ask_groq(language: str, api: str, query: str, docs: dict) -> str:
    """Send the docs + query to Groq and get back code."""
    api_docs = docs.get(api)
    if not api_docs:
        return f"Error: Unknown API '{api}'. Available: {', '.join(docs.keys())}"

    system_prompt = f"""You are mapi, an API code generator. Given API documentation and a user query, return ONLY the code to make the HTTP request.

Rules:
1. Return ONLY code in {language}. No explanation, no markdown fences, just raw code.
2. Use YOUR_API_KEY as the placeholder for the API key.
3. Include comments showing what each parameter does.
4. Make it complete and runnable — include imports, headers, everything.
5. Include error handling (check status code).
6. Print the response.
7. If the request needs a body (POST), include a realistic example body."""

    user_msg = f"""API: {api_docs['service_name']}
Auth: {api_docs['auth_header']}

Available endpoints:
{json.dumps(api_docs['endpoints'], indent=2)}

User wants: {query}
Language: {language}

Return ONLY the {language} code. Nothing else."""

    try:
        resp = httpx.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'model': GROQ_MODEL,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_msg}
                ],
                'temperature': 0.1,
                'max_tokens': 2000
            },
            timeout=30.0
        )

        if resp.status_code != 200:
            return f"Error: Groq returned {resp.status_code}: {resp.text[:200]}"

        content = resp.json()['choices'][0]['message']['content'].strip()

        # Strip markdown fences if Groq wraps them
        if content.startswith('```'):
            lines = content.split('\n')
            content = '\n'.join(lines[1:])
            if content.endswith('```'):
                content = content[:-3]
            content = content.strip()

        return content

    except Exception as e:
        return f"Error: {e}"

# ── Test with Real Key ──────────────────────────────────────────────
def run_test(api_key: str):
    """Run the last cached request with a real API key."""
    cache = load_cache()
    if not cache:
        print(f"  {C.RED}No cached requests to test. Run a query first.{C.RESET}")
        return

    # Get the most recent cache entry
    last_key = list(cache.keys())[-1]
    entry = cache[last_key]

    print(f"\n  {C.BOLD}Testing: {last_key}{C.RESET}")
    print(f"  {C.DIM}Endpoint: {entry.get('endpoint', 'unknown')}{C.RESET}\n")

    code = entry.get('code', '')
    # Replace placeholder with real key
    code_with_key = code.replace('YOUR_API_KEY', api_key)
    code_with_key = code_with_key.replace('YOUR_ACCESS_TOKEN', api_key)

    # Save to temp file and run
    tmp_file = Path('/tmp/mapi_test.py')
    tmp_file.write_text(code_with_key)

    print(f"  {C.DIM}Running...{C.RESET}\n")
    os.system(f'{sys.executable} {tmp_file}')
    print()

# ── Parse Args ──────────────────────────────────────────────────────
def parse_args(args: list[str]) -> dict:
    """
    Parse: mapi -python gemini generate text
    Into: {language: 'python', api: 'gemini', query: 'generate text'}
    """
    if not args:
        return {'error': 'usage'}

    # Handle test command
    if args[0] == 'test':
        for arg in args[1:]:
            if arg.startswith('api_key='):
                return {'command': 'test', 'api_key': arg.split('=', 1)[1].strip('"').strip("'")}
        return {'error': 'test_usage'}

    # Handle help
    if args[0] in ('help', '--help', '-h'):
        return {'error': 'usage'}

    # First arg should be -language
    if not args[0].startswith('-'):
        return {'error': 'usage'}

    language = args[0][1:]  # strip the dash

    if len(args) < 3:
        return {'error': 'usage'}

    api = args[1].lower()
    query = ' '.join(args[2:])

    return {'language': language, 'api': api, 'query': query}

# ── Display ─────────────────────────────────────────────────────────
def print_usage():
    print(f"""
  {C.BOLD}{C.CYAN}mapi{C.RESET} — Find the exact API call you need

  {C.BOLD}Usage:{C.RESET}
    mapi -<language> <api> <what you need>

  {C.BOLD}Examples:{C.RESET}
    mapi -python gemini generate text
    mapi -javascript googlemaps get directions
    mapi -curl gemini chat conversation
    mapi -rust googlemaps geocode address

  {C.BOLD}Test with your API key:{C.RESET}
    mapi test api_key="YOUR_REAL_KEY"

  {C.BOLD}Available APIs:{C.RESET}
    gemini        Google Gemini (text generation, vision, embeddings)
    googlemaps    Google Maps (geocoding, directions, places, distance)

  {C.BOLD}Any language works:{C.RESET} -python, -javascript, -curl, -rust, -go, -java, etc.
""")


def print_result(code: str, verification: dict, language: str, from_cache: bool):
    """Print the generated code with verification status."""

    # Cache status
    if from_cache:
        print(f"\n  {C.GREEN}⚡ Cache hit — instant result{C.RESET}")
    else:
        print(f"\n  {C.BLUE}🤖 Generated by Groq AI{C.RESET}")

    # Verification badge
    v = verification
    if v.get('verified'):
        status = v['status']
        msg = v['message']
        if status in (401, 403):
            print(f"  {C.GREEN}✓ Endpoint verified{C.RESET} ({status}: API key required)")
            print(f"  {C.DIM}API says: \"{msg}\"{C.RESET}")
            print(f"  {C.DIM}→ Endpoint is real. Paste your key to use it.{C.RESET}")
        elif status == 400:
            print(f"  {C.GREEN}✓ Endpoint verified{C.RESET} ({status}: needs valid request body)")
            print(f"  {C.DIM}API says: \"{msg}\"{C.RESET}")
        elif status == 200:
            print(f"  {C.GREEN}✓ Endpoint verified (200 OK){C.RESET}")
        else:
            print(f"  {C.YELLOW}⚠ Endpoint returned {status}{C.RESET}")
            print(f"  {C.DIM}API says: \"{msg}\"{C.RESET}")
    else:
        print(f"  {C.YELLOW}⚠ Could not verify endpoint{C.RESET}")
        if v.get('message'):
            print(f"  {C.DIM}{v['message']}{C.RESET}")

    # Code
    print(f"\n  {C.BOLD}── {language} {'─' * (40 - len(language))}{C.RESET}\n")
    for line in code.split('\n'):
        print(f"  {line}")
    print(f"\n  {C.BOLD}{'─' * 44}{C.RESET}")

    print(f"\n  {C.DIM}To test with your real key:{C.RESET}")
    print(f"  {C.CYAN}mapi test api_key=\"YOUR_REAL_KEY\"{C.RESET}\n")

# ── Main ────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    parsed = parse_args(args)

    # Handle errors
    if 'error' in parsed:
        if parsed['error'] == 'test_usage':
            print(f"\n  {C.RED}Usage: mapi test api_key=\"YOUR_KEY\"{C.RESET}\n")
        else:
            print_usage()
        return

    # Handle test command
    if parsed.get('command') == 'test':
        run_test(parsed['api_key'])
        return

    language = parsed['language']
    api = parsed['api']
    query = parsed['query']

    # Check Groq key
    if not GROQ_API_KEY:
        print(f"\n  {C.RED}✗ GROQ_API_KEY not set{C.RESET}")
        print(f"  Get a free key: {C.CYAN}https://console.groq.com{C.RESET}")
        print(f"  Then add to .env: GROQ_API_KEY=your_key\n")
        return

    # Load docs
    docs = load_docs()
    if api not in docs:
        print(f"\n  {C.RED}✗ Unknown API: '{api}'{C.RESET}")
        print(f"  Available: {C.CYAN}{', '.join(docs.keys())}{C.RESET}\n")
        return

    # Build cache key
    cache_key = f"{language}:{api}:{query}"

    # Check cache
    cache = load_cache()
    if cache_key in cache:
        entry = cache[cache_key]
        print_result(entry['code'], entry.get('verification', {}), language, from_cache=True)
        return

    # Generate with Groq
    print(f"\n  {C.DIM}Generating {language} code for: {api} → {query}...{C.RESET}")
    code = ask_groq(language, api, query, docs)

    if code.startswith('Error:'):
        print(f"\n  {C.RED}{code}{C.RESET}\n")
        return

    # Find the endpoint URL from the generated code for verification
    # Try to extract URL from the code
    endpoint_url = ''
    method = 'GET'
    for ep in docs[api]['endpoints']:
        # Simple match: check if endpoint URL appears in generated code
        if ep['endpoint'] in code:
            endpoint_url = ep['endpoint']
            method = ep['method']
            break

    # Verify endpoint
    print(f"  {C.DIM}Verifying endpoint...{C.RESET}")
    if endpoint_url:
        verification = verify_endpoint(method, endpoint_url)
    else:
        verification = {'verified': False, 'message': 'Could not extract endpoint URL'}

    # Save to cache
    cache[cache_key] = {
        'code': code,
        'endpoint': f"{method} {endpoint_url}" if endpoint_url else 'unknown',
        'verification': verification
    }
    save_cache(cache)

    # Display
    print_result(code, verification, language, from_cache=False)


if __name__ == '__main__':
    main()