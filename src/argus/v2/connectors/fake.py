"""Deterministic test connector. config.signals is a list of {fingerprint,
payload}; the cursor is the index of the next unseen item."""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register


@register
class FakeConnector:
    type = "fake"

    def poll(self, source, state: dict):
        idx = int(state.get("idx", 0))
        items = (source.config or {}).get("signals", [])
        new = [Signal(fingerprint=str(i["fingerprint"]), payload=i.get("payload", {}))
               for i in items[idx:]]
        return new, {"idx": len(items)}
