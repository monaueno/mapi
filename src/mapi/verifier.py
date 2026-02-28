"""
verifier.py — Verify that a generated endpoint actually exists.

Hits the real URL with no auth. If we get 401/403, the endpoint is real
(it just needs a real API key). If we get 404, Groq hallucinated.
"""
import httpx


def verify_endpoint(method: str, url: str) -> dict:
    """
    Hit the endpoint with no real auth to check if it exists.

    Returns:
        {verified: True/False, status: int, message: str}

    Status codes:
        401/403 = endpoint is real, just needs auth  → verified ✓
        400     = endpoint is real, needs valid body  → verified ✓
        200     = endpoint is real and public          → verified ✓
        404     = endpoint doesn't exist               → NOT verified ✗
    """
    clean_url = url.split('?')[0] if method != 'GET' else url

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
        else:
            resp = httpx.request(method, clean_url, timeout=10.0)

        status = resp.status_code

        # Try to extract error message from response
        try:
            body = resp.json()
            if isinstance(body.get('error'), dict):
                msg = body['error'].get('message', str(body['error']))[:200]
            else:
                msg = str(body.get('error', body))[:200]
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