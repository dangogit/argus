"""OpenAI-compatible chat adapter.

Backs the `openrouter` and `ollama` engines - both expose
POST {base}/chat/completions with the OpenAI schema. This frees users from
depending on a vendor agent CLI. Config comes from the environment (no secrets
in YAML); stdlib urllib only, no new dependency.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from argus.engine import EngineOutageError, EngineResult, write_meta


def _timeout() -> float:
    return float(os.environ.get("ARGUS_ENGINE_HTTP_TIMEOUT", "120"))


def _call(prompt: str, *, base_url: str, api_key: str | None, model: str, label: str) -> EngineResult:
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    write_meta("unpriced", "")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise EngineOutageError(f"{label}: {exc.reason}")
    except (TimeoutError, OSError, ValueError) as exc:
        raise EngineOutageError(f"{label}: {exc}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EngineOutageError(f"{label}: unexpected response shape: {exc}")
    return EngineResult(text=(text or "").rstrip("\n"), cost_source="unpriced")


def openrouter(prompt: str) -> EngineResult:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise EngineOutageError("openrouter: OPENROUTER_API_KEY not set")
    base = os.environ.get("ARGUS_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.environ.get("ARGUS_MODEL") or os.environ.get("ARGUS_OPENROUTER_MODEL", "openai/gpt-4o-mini")
    return _call(prompt, base_url=base, api_key=key, model=model, label="openrouter")


def ollama(prompt: str) -> EngineResult:
    # Local Ollama needs no key. ARGUS_MODEL overrides the default model.
    base = os.environ.get("ARGUS_OLLAMA_BASE_URL", "http://localhost:11434/v1")
    model = os.environ.get("ARGUS_MODEL") or os.environ.get("ARGUS_OLLAMA_MODEL", "llama3.1")
    return _call(prompt, base_url=base, api_key=None, model=model, label="ollama")
