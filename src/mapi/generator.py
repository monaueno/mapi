"""
generator.py — Send docs + user query to Groq, get back working code.

This is the core AI step: Groq reads the API docs and the user's plain English
query, then generates complete, runnable code in whatever language they asked for.
"""
import json
import re

import httpx

from .config import GROQ_API_KEY, GROQ_MODEL


def clean_curl_output(code: str) -> str:
    """Strip pipes, -w flags, and subshells from generated curl commands.

    Groq likes to add things like:
      -w "%{http_code}" | if [ $? -eq 0 ]; then cat; else echo "Error"; fi
    which cause "Failed writing body" because the pipe consumer closes early.
    """
    # Remove -w "..." or -w '...' flags
    code = re.sub(r"""\s*-w\s+['"]?%\{[^}]*\}['"]?""", '', code)
    # Walk the entire string tracking quotes to find the first unquoted |
    in_single = False
    in_double = False
    pipe_pos = None
    for i, ch in enumerate(code):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == '|' and not in_single and not in_double:
            pipe_pos = i
            break
        elif ch == '&' and not in_single and not in_double and i + 1 < len(code) and code[i + 1] == '&':
            pipe_pos = i
            break
    if pipe_pos is not None:
        code = code[:pipe_pos]
    # Remove trailing backslash on the now-final line
    lines = code.rstrip().split('\n')
    while lines and lines[-1].rstrip().endswith('\\'):
        lines[-1] = lines[-1].rstrip().rstrip('\\').rstrip()
        if not lines[-1].strip():
            lines.pop()
    return '\n'.join(lines).strip()


def _groq_chat(messages: list, *, temperature: float = 0.1, max_tokens: int = 2000):
    """Low-level Groq chat call. Returns the message content string or raises."""
    resp = httpx.post(
        'https://api.groq.com/openai/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {GROQ_API_KEY}',
            'Content-Type': 'application/json'
        },
        json={
            'model': GROQ_MODEL,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Groq returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()['choices'][0]['message']['content'].strip()


def _strip_fences(content: str) -> str:
    """Remove ```lang fences that models sometimes wrap output in."""
    if content.startswith('```'):
        lines = content.split('\n')
        content = '\n'.join(lines[1:])
        if content.endswith('```'):
            content = content[:-3]
        content = content.strip()
    return content


_STOPWORDS = {
    'the', 'a', 'an', 'to', 'for', 'of', 'and', 'or', 'my', 'me', 'i', 'we',
    'in', 'on', 'with', 'all', 'this', 'that', 'get', 'please', 'want', 'need',
    'new', 'up', 'it', 'as', 'from', 'by', 'is', 'are', 'do', 'can', 'you',
}


def _tokens(text: str) -> set:
    return {w for w in re.findall(r'[a-z0-9]+', (text or '').lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _catalog_entries(docs: dict) -> list:
    """Compact, body-free endpoint entries (with true index as `id`)."""
    entries = []
    for i, ep in enumerate(docs.get('endpoints', [])):
        entries.append({
            'id': i,
            'action': ep.get('action', ''),
            'method': ep.get('method', ''),
            'endpoint': ep.get('endpoint', ''),
            'description': (ep.get('description') or '')[:140],
            'params': ep.get('params', ''),
            'tags': ep.get('tags', []),
        })
    return entries


def _endpoint_path_words(url: str) -> str:
    """Resource words from an endpoint path — the endpoint's real "name".

    Drops the host, version/api segments, and path placeholders so what's left
    is the resource nouns, e.g.
        https://app.jobnimbus.com/api1/v2/materialorders/<jnid>  ->  "materialorders"
    """
    path = re.sub(r'^https?://[^/]+', '', url)
    words = []
    for seg in re.split(r'[/:]', path):
        seg = seg.strip()
        if not seg or '<' in seg or '{' in seg:
            continue
        if re.fullmatch(r'v?\d+|api\d*', seg):  # version / api1 / v2 segments
            continue
        words.append(seg)
    return ' '.join(words)


def _word_match(qw: str, hw: str) -> bool:
    """Match two words tolerating singular/plural and stems (contact ~ contacts)."""
    if qw == hw:
        return True
    if len(qw) >= 4 and len(hw) >= 4 and (qw.startswith(hw) or hw.startswith(qw)):
        return True
    return False


def _prefilter(query: str, entries: list, limit: int) -> list:
    """Narrow a large catalog to the most relevant entries.

    Scores each endpoint against the query using stem-aware matching so
    singular/plural forms line up (e.g. "contact" matches the `contacts`
    endpoint). Matches on the endpoint's name — its path and action — count
    more than matches in the free-text description. Query words that are just
    values ("alex") match nothing here and are left for the LLM to extract as
    params. Keeps token usage under Groq's per-minute budget on big APIs, and
    falls back to the full head of the list when nothing scores so we never
    starve the resolver.
    """
    if len(entries) <= limit:
        return entries
    qwords = _tokens(query)
    if not qwords:
        return entries[:limit]

    scored = []
    for e in entries:
        # "name" = the endpoint's identity: its path resource + action + tags.
        name_tokens = _tokens(' '.join([
            e['action'],
            ' '.join(e.get('tags', [])),
            _endpoint_path_words(e['endpoint']),
        ]))
        desc_tokens = _tokens(e.get('description', ''))

        score = 0
        for qw in qwords:
            if any(_word_match(qw, h) for h in name_tokens):
                score += 2  # a hit on the endpoint's name is a strong signal
            elif any(_word_match(qw, h) for h in desc_tokens):
                score += 1
        # small nudge so create/update/list verbs in the query bias the method
        if 'create' in qwords and e['method'] == 'POST':
            score += 1
        if ('update' in qwords or 'edit' in qwords) and e['method'] in ('PUT', 'PATCH'):
            score += 1
        scored.append((score, e))

    scored.sort(key=lambda s: s[0], reverse=True)
    top = [e for score, e in scored if score > 0][:limit]
    return top if top else entries[:limit]


def resolve_intent(query: str, docs: dict) -> dict | None:
    """Read the user's plain-English request and pick the single best endpoint.

    This is the "understand what the user wants" step. It runs BEFORE code
    generation so the generator only sees the one endpoint that matters, and so
    mapi can show the user what it understood (and how confident it is).

    Returns a dict with keys: understood, method, endpoint, action, confidence,
    extracted_params, notes, id — or None if resolution fails (caller falls back
    to whole-catalog generation).
    """
    service = docs.get('service_name', 'this API')
    system_prompt = f"""You are mapi's intent resolver for the {service}.

Your job: read the user's plain-English request and pick the SINGLE best API endpoint from the catalog.

Think carefully about:
- Synonyms and paraphrases ("pull customers" ~= list/retrieve contacts; "make a job" ~= create a job).
- CRUD intent: create / get / list / update / delete.
- Resource type: contact, job, task, invoice, estimate, product, file, activity, etc.
- Prefer a "list/retrieve all" endpoint for show/get/list/fetch/pull requests, a single-item GET when an id is given.
- Prefer POST for create/add/new, PUT for update/edit/change/delete-by-flag, DELETE for remove.
- Pull any concrete values in the request (ids, names, dates, statuses) into extracted_params.

Return ONLY valid JSON (no markdown) with this exact shape:
{{
  "id": <the integer id of the chosen endpoint from the catalog>,
  "understood": "one clear sentence restating what the user wants",
  "confidence": "high | medium | low",
  "extracted_params": {{}},
  "notes": "brief note on assumptions or missing required fields, or empty string"
}}

Rules:
- "id" MUST be one of the ids in the catalog.
- extracted_params: only values the user actually provided or clearly implied; use realistic key names.
- If several endpoints could fit, choose the most specific and set confidence to "medium".
- If nothing fits well, still choose the closest and set confidence to "low"."""

    entries = _prefilter(query, _catalog_entries(docs), limit=25)
    user_msg = f"""Endpoint catalog:
{json.dumps(entries, indent=2)}

User request: {query}

Return ONLY the JSON object."""

    try:
        content = _groq_chat(
            [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_msg},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        content = _strip_fences(content)
        data = json.loads(content)
    except Exception:
        return None

    endpoints = docs.get('endpoints', [])
    idx = data.get('id')
    if not isinstance(idx, int) or not (0 <= idx < len(endpoints)):
        return None

    ep = endpoints[idx]
    return {
        'id': idx,
        'understood': data.get('understood', ''),
        'confidence': (data.get('confidence') or 'medium').lower(),
        'extracted_params': data.get('extracted_params') or {},
        'notes': data.get('notes', ''),
        'action': ep.get('action', ''),
        'method': ep.get('method', 'GET'),
        'endpoint': ep.get('endpoint', ''),
    }


def ask_groq(language: str, query: str, docs: dict, intent: dict | None = None) -> str:
    """Generate code for an API call using Groq.

    If `intent` is provided (from resolve_intent), the generator is focused on
    that single resolved endpoint and told what the user meant plus any
    extracted params. Otherwise it falls back to the full endpoint catalog.
    """
    system_prompt = f"""You are mapi, an API code generator. Given API documentation and a user query, return ONLY the code to make the HTTP request.

Rules:
1. Return ONLY code in {language}. No explanation, no markdown fences, no comments, just raw executable code.
2. Use YOUR_API_KEY as the placeholder for the API key.
3. Do NOT include any comments in the code. No inline comments, no block comments, no # or // or /* lines.
4. Make it complete and runnable — include imports, headers, everything.
5. Include error handling (check status code).
6. Print the response.
7. If the endpoint has an example_body, use that EXACT JSON structure in the request. Substitute the user's query and any extracted params into the relevant fields but keep the nesting identical.
8. JSON strings and request bodies must be valid JSON with no comments or trailing content.
9. Substitute any extracted parameter values (ids, names, dates) into the URL path and body where they belong. Replace placeholders like <jnid>, :id, or {{id}} with the real value when provided, otherwise leave a clear YOUR_ID placeholder.
10. For curl/bash: do NOT pipe curl output through anything. No | pipes, no -w flag, no subshells. Just a plain curl command that prints the response to stdout."""

    if intent and intent.get('endpoint'):
        chosen = docs['endpoints'][intent['id']]
        user_msg = f"""API: {docs['service_name']}
Auth: {docs.get('auth', 'See documentation')}

Use THIS endpoint (already resolved from the user's request):
{json.dumps(chosen, indent=2)}

What the user wants: {intent.get('understood') or query}
Original request: {query}
Extracted params: {json.dumps(intent.get('extracted_params', {}))}
Language: {language}

IMPORTANT:
- Use the EXACT auth method described above. Do not use Authorization: Bearer unless the auth info says so.
- Use the endpoint URL and method exactly as given. Substitute extracted params into the path/body.
- If the endpoint has an "example_body", use that exact JSON structure as the request body. Do not flatten or simplify it.
Return ONLY the {language} code. Nothing else."""
    else:
        # Fallback (intent resolution unavailable): send a compact, body-free
        # catalog narrowed to the most relevant endpoints so we stay within the
        # token budget instead of dumping every endpoint and body.
        entries = _prefilter(query, _catalog_entries(docs), limit=15)
        user_msg = f"""API: {docs['service_name']}
Auth: {docs.get('auth', 'See documentation')}

Available endpoints:
{json.dumps(entries, indent=2)}

User wants: {query}
Language: {language}

IMPORTANT:
- Pick the single best endpoint for the request and generate one call to it.
- Use the EXACT auth method described above. Do not use Authorization: Bearer unless the auth info says to.
Return ONLY the {language} code. Nothing else."""

    try:
        content = _groq_chat(
            [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_msg},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        # Strip markdown fences if Groq wraps them
        content = _strip_fences(content)
        # Strip pipes and -w flags that break curl output
        if language in ('curl', 'bash'):
            content = clean_curl_output(content)
        return content

    except Exception as e:
        return f"Error: {e}"