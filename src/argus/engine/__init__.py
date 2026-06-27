"""Engine subsystem: adapters, router, and cost provenance.

Exit-code contract: 0 success, 3 unknown engine, 42 engine outage (OUTAGE_RC).
Cost source is one of exact, estimated, or unpriced.
"""
import os
from dataclasses import dataclass
from pathlib import Path

OUTAGE_RC = 42
UNKNOWN_ENGINE_RC = 3

_COST_SOURCES = ("exact", "estimated", "unpriced")


class UnknownEngineError(Exception):
    """Raised for an engine name with no adapter (bash exit 3)."""


class EngineOutageError(Exception):
    """Raised when an adapter's CLI is missing or hard-fails (bash exit 42)."""


@dataclass(frozen=True)
class EngineResult:
    text: str
    cost_source: str = "unpriced"
    cost_usd: str = ""

    def __post_init__(self):
        if self.cost_source not in _COST_SOURCES:
            raise ValueError(f"invalid costSource: {self.cost_source}")


def write_meta(cost_source: str, cost_usd: str = "") -> None:
    """Write cost provenance to the ARGUS_ENGINE_META sink, if one is set.

    Mirrors argus_engine_emit_meta: adapters call this BEFORE the agent runs so
    provenance survives an outage. It is a no-op without the env so adapters stay
    usable outside the metadata-aware caller.
    """
    if cost_source not in _COST_SOURCES:
        raise ValueError(f"invalid costSource: {cost_source}")
    sink = os.environ.get("ARGUS_ENGINE_META")
    if not sink:
        return
    Path(sink).write_text(f"costSource={cost_source}\ncostUsd={cost_usd}\n", encoding="utf-8")


def run_agent(engine: str, prompt: str) -> EngineResult:
    """Dispatch to the named adapter. Resets the meta sink first so each
    call's provenance is clean."""
    from argus.engine.adapters import ADAPTERS

    try:
        adapter = ADAPTERS[engine]
    except KeyError:
        raise UnknownEngineError(f"unknown engine: {engine}")
    sink = os.environ.get("ARGUS_ENGINE_META")
    if sink:
        Path(sink).write_text("")
    return adapter(prompt)
