"""
generator.py — Send docs + user query to Groq, get back working code.

This is the core AI step: Groq reads the API docs and the user's plain English
query, then generates complete, runnable code in whatever language they asked for.
"""
import json

import httpx

from .config import GROQ_API_KEY, GROQ_MODEL


def ask_groq(language: str, query: str, docs: dict) -> str:
    """Generate code for an API call using Groq."""
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