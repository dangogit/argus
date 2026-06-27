"""Embeddings. Default = a deterministic offline fake (gate-testable). A real
provider is a seam selected by company.defaults.embedder."""
from __future__ import annotations

import hashlib
from typing import Optional

_DIM = 64


def _fake_embed(text: str) -> list:
    v = [0.0] * _DIM
    for tok in (text or "").lower().split():
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % _DIM] += 1.0
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


def embed(text: str, cfg=None) -> Optional[list]:
    spec = getattr(cfg.company.defaults, "embedder", None) if cfg else None
    if spec:  # pragma: no cover
        return _real_embed(text, spec)
    return _fake_embed(text)


def _real_embed(text: str, spec):  # pragma: no cover
    # SEAM -- an embeddings provider (OpenAI-compatible API or local model).
    import httpx
    r = httpx.post(spec.get("url"), headers={"Authorization": f"Bearer {spec.get('secret')}"},
                   json={"model": spec.get("model"), "input": text}, timeout=30)
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]


def vec_literal(vec) -> Optional[str]:
    return None if vec is None else "[" + ",".join(repr(float(x)) for x in vec) + "]"
