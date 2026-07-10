"""Connector framework: a Signal, a Connector protocol, and a self-registering
registry keyed by SourceRef.type."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

REGISTRY: dict[str, type] = {}


@dataclass
class Signal:
    fingerprint: str
    payload: dict = field(default_factory=dict)
    # Optional origin descriptor for respond-back (orchestrator/respond.py):
    # where to post the outcome once this signal's work goes terminal, e.g.
    # {"kind": "supabase_bug_reports", "row_id": ...} or
    # {"kind": "slack_thread", "channel": ..., "ts": ...}. Persisted into the
    # event payload as 'reply_to' by the driver. Connectors populate it where
    # they can; None is always fine.
    reply_to: dict | None = None


def register(cls):
    REGISTRY[cls.type] = cls
    return cls


@runtime_checkable
class Connector(Protocol):
    type: str

    def poll(self, source, state: dict) -> "tuple[list[Signal], dict]":
        ...
