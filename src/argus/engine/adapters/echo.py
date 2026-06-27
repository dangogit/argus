"""Deterministic test engine. No external calls. Used by tests and CI."""
from argus.engine import EngineResult, write_meta


def run(prompt: str) -> EngineResult:
    write_meta("unpriced", "")
    return EngineResult(text=f"ECHO: {prompt}", cost_source="unpriced")
