"""Shared HTTP helper for connectors: one timeout/raise_for_status convention,
plus a failure classifier so the driver can tell a permanent auth failure
(bad/expired key, needs a human) apart from a transient one (timeout, 5xx,
network blip, worth retrying on the normal backoff)."""
from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 20.0


def fetch_json(url: str, *, headers: dict | None = None, params: dict | None = None,
                timeout: float = DEFAULT_TIMEOUT):
    """GET a URL and return the parsed JSON body. Raises httpx.HTTPStatusError
    on a non-2xx response (classify() below turns that into a category)."""
    r = httpx.get(url, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def classify(exc: Exception) -> str:
    """Bucket a connector failure so the driver can treat it differently:
    - 'auth': 401/403, permanent until a human rotates the key. Never worth
      retrying on the normal backoff.
    - 'rate_limit': 429, transient but the provider is asking us to slow down.
    - 'transient': timeouts, network errors, 5xx. Worth the normal backoff.
    - 'other': anything else (4xx that isn't auth/rate-limit, parsing errors).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "auth"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "transient"
        return "other"
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return "transient"
    return "other"
