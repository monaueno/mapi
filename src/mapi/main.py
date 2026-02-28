"""
main.py — Entry point. Parses args and orchestrates the flow.

Flow:
    1. Parse command:  mapi -python gemini generate text
    2. Check cache  →  cache hit? print and done
    3. Load docs    →  not scraped yet? scraper.py handles it
    4. Generate     →  Groq creates the code
    5. Verify       →  hit the real endpoint to confirm it exists
    6. Cache        →  save for next time
    7. Display      →  print the code with verification badge
"""
import os
import sys
from pathlib import Path

from .config import GROQ_API_KEY, API_SOURCES
from .cache import load_cache, save_cache
from .scraper import load_api_docs
from .generator import ask_groq
from .verifier import verify_endpoint
from .display import C, print_usage, print_result


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


def run_test(api_key):
    """Run the last cached request with a real API key."""
    cache = load_cache()
    if not cache:
        print(f"  {C.RED}No cached requests to test. Run a query first.{C.RESET}")
        return

    last_key = list(cache.keys())[-1]
    entry = cache[last_key]

    print(f"\n  {C.BOLD}Testing: {last_key}{C.RESET}")
    print(f"  {C.DIM}Endpoint: {entry.get('endpoint', 'unknown')}{C.RESET}\n")

    code = entry.get('code', '')
    code_with_key = code.replace('YOUR_API_KEY', api_key).replace('YOUR_ACCESS_TOKEN', api_key)

    tmp_file = Path('/tmp/mapi_test.py')
    tmp_file.write_text(code_with_key)

    print(f"  {C.DIM}Running...{C.RESET}\n")
    os.system(f'{sys.executable} {tmp_file}')
    print()


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

    # ── Step 1: Check cache ─────────────────────────────────────────
    cache_key = f"{language}:{api}:{query}"
    cache = load_cache()
    if cache_key in cache:
        entry = cache[cache_key]
        print_result(entry['code'], entry.get('verification', {}), language, from_cache=True)
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

    # ── Step 6: Save to cache ───────────────────────────────────────
    cache[cache_key] = {
        'code': code,
        'endpoint': f"{method} {endpoint_url}" if endpoint_url else 'unknown',
        'verification': verification
    }
    save_cache(cache)

    # ── Step 7: Display ─────────────────────────────────────────────
    print_result(code, verification, language, from_cache=False)


if __name__ == '__main__':
    main()