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


def ask_groq(language: str, query: str, docs: dict) -> str:
    """Generate code for an API call using Groq."""
    system_prompt = f"""You are mapi, an API code generator. Given API documentation and a user query, return ONLY the code to make the HTTP request.

Rules:
1. Return ONLY code in {language}. No explanation, no markdown fences, no comments, just raw executable code.
2. Use YOUR_API_KEY as the placeholder for the API key.
3. Do NOT include any comments in the code. No inline comments, no block comments, no # or // or /* lines.
4. Make it complete and runnable — include imports, headers, everything.
5. Include error handling (check status code).
6. Print the response.
7. If the endpoint has an example_body, use that EXACT JSON structure in the request. Substitute the user's query into the text/content fields but keep the nesting identical.
8. JSON strings and request bodies must be valid JSON with no comments or trailing content.
9. For curl/bash: do NOT pipe curl output through anything. No | pipes, no -w flag, no subshells. Just a plain curl command that prints the response to stdout."""

    user_msg = f"""API: {docs['service_name']}
Auth: {docs.get('auth', 'See documentation')}

Available endpoints:
{json.dumps(docs['endpoints'], indent=2)}

User wants: {query}
Language: {language}

IMPORTANT:
- Use the EXACT auth method described above. Do not use Authorization: Bearer unless the auth info says to.
- If the chosen endpoint has an "example_body", use that exact JSON structure as the request body. Do not flatten or simplify it.
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
        # Strip pipes and -w flags that break curl output
        if language in ('curl', 'bash'):
            content = clean_curl_output(content)
        return content

    except Exception as e:
        return f"Error: {e}"