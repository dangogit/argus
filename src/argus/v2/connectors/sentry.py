"""Sentry issues connector. parse() (pure, gate-tested) filters unresolved
issues at/above min_level seen after the cursor; fetch() (network, not gate-
tested) GETs the Issues API. fingerprint = Sentry issue id."""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register

_LEVELS = {"debug": 0, "info": 1, "warning": 2, "error": 3, "fatal": 4}


@register
class SentryConnector:
    type = "sentry"

    @staticmethod
    def parse(raw, state: dict, *, min_level: str = "error"):
        floor = _LEVELS.get(min_level, 3)
        last_seen = state.get("last_seen") or ""
        signals, newest = [], last_seen
        for issue in raw:
            level = _LEVELS.get(issue.get("level", "error"), 3)
            seen = issue.get("lastSeen", "")
            if level < floor or seen <= last_seen:
                continue
            signals.append(Signal(
                fingerprint=str(issue["id"]),
                payload={"title": issue.get("title"), "level": issue.get("level"),
                         "permalink": issue.get("permalink"), "count": issue.get("count")}))
            newest = max(newest, seen)
        return signals, {"last_seen": newest}

    def fetch(self, source, state: dict):
        # NETWORK -- not run in the gate. GET the unresolved issues, newest first.
        import httpx
        cfg = source.config or {}
        base = cfg.get("base_url", "https://sentry.io")  # region URL, e.g. https://de.sentry.io
        url = f"{base}/api/0/projects/{cfg['org']}/{cfg['project']}/issues/"
        r = httpx.get(url, params={"query": "is:unresolved", "sort": "date"},
                      headers={"Authorization": f"Bearer {source.secret}"}, timeout=20)
        r.raise_for_status()
        return r.json()

    def poll(self, source, state: dict):
        raw = self.fetch(source, state)
        return self.parse(raw, state, min_level=(source.config or {}).get("min_level", "error"))
