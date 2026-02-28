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
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent / 'data'
CACHE_PATH = DATA_DIR / 'cache.json'
FIRECRAWL_API_KEY = os.environ.get('fc_api_key', '')
GROQ_API_KEY = os.environ.get('groq_api_key', '')
GROQ_MODEL = 'llama-3.3-70b-versatile'

API_SOURCES = {
    "googlemaps": {
        "name": "Google Maps Platform",
        "urls": [
            "https://developers.google.com/maps/documentation/geocoding/requests-geocoding",
            "https://developers.google.com/maps/documentation/directions/get-directions",
            "https://developers.google.com/maps/documentation/places/web-service/nearby-search",
            "https://developers.google.com/maps/documentation/places/web-service/place-details",
            "https://developers.google.com/maps/documentation/distancematrix/distance-matrix",
        ]
    },
    "gemini": {
        "name": "Google Gemini API",
        "urls": [
            "https://ai.google.dev/gemini-api/docs/quickstart",
            "https://ai.google.dev/api/generate-content",
            "https://ai.google.dev/api/embeddings",
            "https://ai.google.dev/api/models",
        ]
    },
}

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


# ═══════════════════════════════════════════════════════════════════
# FIRECRAWL SCRAPING
# ═══════════════════════════════════════════════════════════════════
def scrape_url(url):
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


def parse_scraped_docs(markdown, api_name):
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


def scrape_api_docs(api):
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


# ═══════════════════════════════════════════════════════════════════
# LOAD DOCS (auto-scrape if missing)
# ═══════════════════════════════════════════════════════════════════
def load_api_docs(api):
    docs_file = DATA_DIR / api / 'docs.json'
    if docs_file.exists():
        with open(docs_file) as f:
            docs = json.load(f)
        if docs.get('endpoints'):
            return docs
    return scrape_api_docs(api)


# ═══════════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════════
def load_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)


# ═══════════════════════════════════════════════════════════════════
# VERIFY ENDPOINT
# ═══════════════════════════════════════════════════════════════════
def verify_endpoint(method, url):
    clean_url = url.split('?')[0] if method != 'GET' else url
    if method == 'GET' and '?' not in clean_url:
        clean_url += '?key=INVALID_KEY'
    elif method == 'GET' and 'key=' not in clean_url:
        clean_url += '&key=INVALID_KEY'

    try:
        if method in ('GET',):
            resp = httpx.get(clean_url, timeout=10.0)
        elif method == 'POST':
            resp = httpx.post(clean_url, headers={'Content-Type': 'application/json'}, json={}, timeout=10.0)
        else:
            resp = httpx.request(method, clean_url, timeout=10.0)

        status = resp.status_code
        try:
            body = resp.json()
            msg = body.get('error', {}).get('message', str(body)[:200]) if isinstance(body.get('error'), dict) else str(body.get('error', body))[:200]
        except Exception:
            msg = resp.text[:200]

        if status in (401, 403):
            return {'verified': True, 'status': status, 'message': msg}
        elif status == 404:
            return {'verified': False, 'status': 404, 'message': msg}
        elif status == 400:
            return {'verified': True, 'status': 400, 'message': msg}
        elif status == 200:
            return {'verified': True, 'status': 200, 'message': 'OK'}
        else:
            return {'verified': True, 'status': status, 'message': msg}
    except Exception as e:
        return {'verified': False, 'status': 0, 'message': str(e)}


# ═══════════════════════════════════════════════════════════════════
# GROQ CODE GENERATION
# ═══════════════════════════════════════════════════════════════════
def ask_groq(language, query, docs):
    system_prompt = f"""You are mapi, an API code generator. Given API documentation and a user query, return ONLY the code to make the HTTP request.

Rules:
1. Return ONLY code in {language}. No explanation, no markdown fences, just raw code.
2. Use YOUR_API_KEY as the placeholder for the API key.
3. Include comments showing what each parameter does.
4. Make it complete and runnable — include imports, headers, everything.
5. Include error handling (check status code).
6. Print the response.
7. If the request needs a body (POST), include a realistic example body."""

    user_msg = f"""API: {docs['service_name']}
Auth: {docs.get('auth_header', 'See documentation')}

Available endpoints:
{json.dumps(docs['endpoints'], indent=2)}

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
        if content.startswith('```'):
            lines = content.split('\n')
            content = '\n'.join(lines[1:])
            if content.endswith('```'):
                content = content[:-3]
            content = content.strip()
        return content
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════════════
# TEST WITH REAL KEY
# ═══════════════════════════════════════════════════════════════════
def run_test(api_key):
    cache = load_cache()
    if not cache:
        print(f"  {C.RED}No cached requests to test. Run a query first.{C.RESET}")
        return
    last_key = list(cache.keys())[-1]
    entry = cache[last_key]
    print(f"\n  {C.BOLD}Testing: {last_key}{C.RESET}")
    print(f"  {C.DIM}Endpoint: {entry.get('endpoint', 'unknown')}{C.RESET}\n")
    code = entry.get('code', '').replace('YOUR_API_KEY', api_key).replace('YOUR_ACCESS_TOKEN', api_key)
    tmp_file = Path('/tmp/mapi_test.py')
    tmp_file.write_text(code)
    print(f"  {C.DIM}Running...{C.RESET}\n")
    os.system(f'{sys.executable} {tmp_file}')
    print()


# ═══════════════════════════════════════════════════════════════════
# PARSE ARGS & DISPLAY
# ═══════════════════════════════════════════════════════════════════
def parse_args(args):
    if not args:
        return {'error': 'usage'}
    if args[0] == 'test':
        for arg in args[1:]:
            if arg.startswith('api_key='):
                return {'command': 'test', 'api_key': arg.split('=', 1)[1].strip('"').strip("'")}
        return {'error': 'test_usage'}
    if args[0] in ('help', '--help', '-h'):
        return {'error': 'usage'}
    if not args[0].startswith('-'):
        return {'error': 'usage'}
    language = args[0][1:]
    if len(args) < 3:
        return {'error': 'usage'}
    api = args[1].lower()
    query = ' '.join(args[2:])
    return {'language': language, 'api': api, 'query': query}


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


def print_result(code, verification, language, from_cache):
    if from_cache:
        print(f"\n  {C.GREEN}⚡ Cache hit — instant result{C.RESET}")
    else:
        print(f"\n  {C.BLUE}🤖 Generated by Groq AI{C.RESET}")

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

    print(f"\n  {C.BOLD}── {language} {'─' * (40 - len(language))}{C.RESET}\n")
    for line in code.split('\n'):
        print(f"  {line}")
    print(f"\n  {C.BOLD}{'─' * 44}{C.RESET}")
    print(f"\n  {C.DIM}To test with your real key:{C.RESET}")
    print(f"  {C.CYAN}mapi test api_key=\"YOUR_REAL_KEY\"{C.RESET}\n")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════
def main():
    args = sys.argv[1:]
    parsed = parse_args(args)

    if 'error' in parsed:
        if parsed['error'] == 'test_usage':
            print(f"\n  {C.RED}Usage: mapi test api_key=\"YOUR_KEY\"{C.RESET}\n")
        else:
            print_usage()
        return

    if parsed.get('command') == 'test':
        run_test(parsed['api_key'])
        return

    language = parsed['language']
    api = parsed['api']
    query = parsed['query']

    if not GROQ_API_KEY:
        print(f"\n  {C.RED}✗ GROQ_API_KEY not set{C.RESET}")
        print(f"  Get a free key: {C.CYAN}https://console.groq.com{C.RESET}")
        print(f"  Add to .env: GROQ_API_KEY=gsk_your_key\n")
        return

    if api not in API_SOURCES:
        print(f"\n  {C.RED}✗ Unknown API: '{api}'{C.RESET}")
        print(f"  Available: {C.CYAN}{', '.join(API_SOURCES.keys())}{C.RESET}\n")
        return

    cache_key = f"{language}:{api}:{query}"

    cache = load_cache()
    if cache_key in cache:
        entry = cache[cache_key]
        print_result(entry['code'], entry.get('verification', {}), language, from_cache=True)
        return

    docs = load_api_docs(api)
    if not docs or not docs.get('endpoints'):
        print(f"\n  {C.RED}✗ No docs available for '{api}'{C.RESET}")
        print(f"  Make sure FIRECRAWL_API_KEY is set in .env\n")
        return

    print(f"\n  {C.DIM}Generating {language} code for: {api} → {query}...{C.RESET}")
    code = ask_groq(language, query, docs)

    if code.startswith('Error:'):
        print(f"\n  {C.RED}{code}{C.RESET}\n")
        return

    endpoint_url = ''
    method = 'GET'
    for ep in docs['endpoints']:
        if ep['endpoint'] in code:
            endpoint_url = ep['endpoint']
            method = ep['method']
            break

    print(f"  {C.DIM}Verifying endpoint...{C.RESET}")
    if endpoint_url:
        verification = verify_endpoint(method, endpoint_url)
    else:
        verification = {'verified': False, 'message': 'Could not extract endpoint URL'}

    cache[cache_key] = {
        'code': code,
        'endpoint': f"{method} {endpoint_url}" if endpoint_url else 'unknown',
        'verification': verification
    }
    save_cache(cache)

    print_result(code, verification, language, from_cache=False)


if __name__ == '__main__':
    main()