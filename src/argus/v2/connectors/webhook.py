"""Generic JSON webhook connector."""
from __future__ import annotations

import hashlib
import json

from argus.v2.connectors.base import Signal, register


@register
class WebhookConnector:
    type = "webhook"

    @staticmethod
    def parse(raw, state: dict, *, source_name: str):
        last_seen = str(state.get("last_seen") or "")
        items = _items(raw)
        signals: list[Signal] = []
        newest = last_seen
        for item in items:
            seen = str(item.get("ts") or item.get("timestamp") or item.get("created_at") or "")
            if seen and seen <= last_seen:
                continue
            fingerprint = str(
                item.get("fingerprint")
                or item.get("id")
                or f"webhook-{source_name}-{_digest(item)}"
            )
            payload = dict(item)
            payload.setdefault("source", "webhook")
            payload.setdefault("kind", "incident")
            signals.append(Signal(fingerprint=fingerprint, payload=payload))
            if seen:
                newest = max(newest, seen)
        return signals, {"last_seen": newest}

    def fetch(self, source, state: dict):  # pragma: no cover
        cfg = source.config or {}
        if "events" in cfg:
            return cfg["events"]
        url = cfg.get("url", "")
        if not url:
            return []
        import httpx

        headers = dict(cfg.get("headers") or {})
        if source.secret:
            headers.setdefault("Authorization", f"Bearer {source.secret}")
        response = httpx.get(url, headers=headers, timeout=float(cfg.get("timeout", 20)))
        response.raise_for_status()
        return response.json()

    def poll(self, source, state: dict):
        return self.parse(self.fetch(source, state), state, source_name=source.name)


def _items(raw) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("signals", "events", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [raw]
    return []


def _digest(item: dict) -> str:
    payload = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
