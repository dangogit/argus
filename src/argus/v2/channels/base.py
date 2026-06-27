"""Channel adapter framework: an InboundMessage, a ChannelAdapter protocol, and
a self-registering registry keyed by ChannelBinding.type."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

REGISTRY: dict[str, type] = {}


@dataclass
class InboundMessage:
    chat_id: str
    text: str
    dedup_key: str
    sender: str = ""
    sender_ref: str = ""
    media: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def register(cls):
    REGISTRY[cls.type] = cls
    return cls


@runtime_checkable
class ChannelAdapter(Protocol):
    type: str

    def parse_inbound(self, raw, secret=None) -> "list[InboundMessage]": ...
    def send(self, binding, text: str) -> str: ...
