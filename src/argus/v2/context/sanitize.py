"""Context sanitizers ported from the legacy personal-context guards."""
from __future__ import annotations

import re

_PHRASES = [
    re.compile(r"ignore (?:previous|all|above) instructions", re.I),
    re.compile(r"disregard (?:previous|all|the) (?:instructions|context)", re.I),
    re.compile(r"you are now [^.]*", re.I),
    re.compile(r"system prompt:", re.I),
]

_PROMISE = re.compile(
    r"\bi'll\b|\bi\u2019ll\b|\bi will\b|\bi'm going to\b|"
    r"\bi\u2019m going to\b|\bim going to\b|"
    r"\bi can (?:send|do|call|share|finish|deliver)\b|"
    r"\bwe'll\b|\bwe\u2019ll\b|\bwe will\b",
    re.I,
)

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.I,
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I)
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|access[_-]?key|auth[_-]?token|token|password|passwd|pwd|secret)"
    r"\s*(?:=|:)\s*(?:['\"])?[^\s,'\";]+(?:['\"])?",
    re.I,
)


def sanitize(text: str) -> str:
    out = re.sub(r"```[\s\S]*?```", " ", text or "")
    out = re.sub(r"<<<[^>]*>>>", " ", out)
    for pattern in _PHRASES:
        out = pattern.sub(" ", out)
    out = out.replace("\n", " ")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip(" \t")


def sanitize_memory(text: str) -> str:
    """Make untrusted activity safe for model prompts and durable summaries."""
    out = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text or "")
    out = _BEARER.sub("Bearer [REDACTED]", out)
    out = _AWS_KEY.sub("[REDACTED AWS KEY]", out)
    out = _CREDENTIAL_ASSIGNMENT.sub("[REDACTED CREDENTIAL]", out)
    return sanitize(out)


def neutralize_fence(text: str) -> str:
    return (text or "").replace("<<<MSG", "<<<msg").replace("MSG>>>", "msg>>>")


def looks_like_commitment(text: str) -> bool:
    return bool(_PROMISE.search(text or ""))
