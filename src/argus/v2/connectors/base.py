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


def register(cls):
    REGISTRY[cls.type] = cls
    return cls


@runtime_checkable
class Connector(Protocol):
    type: str

    def poll(self, source, state: dict) -> "tuple[list[Signal], dict]":
        ...
