"""HTTP uptime connector: emits one signal when a target transitions down."""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register


@register
class UptimeConnector:
    type = "uptime"

    @staticmethod
    def parse(status: str, state: dict, *, project: str, url: str = ""):
        normalized = "up" if status == "up" else "down"
        if normalized == "up":
            return [], {"last_status": "up"}
        cursor = {"last_status": "down"}
        if state.get("last_status") == "down":
            return [], cursor
        fingerprint = f"uptime-{project}"
        return [
            Signal(
                fingerprint=fingerprint,
                payload={
                    "source": "uptime",
                    "severity": "error",
                    "fingerprint": fingerprint,
                    "message": f"uptime check for {project} is DOWN",
                    "url": url,
                    "kind": "incident",
                },
            )
        ], cursor

    def fetch(self, source, state: dict) -> str:  # pragma: no cover
        import httpx

        cfg = source.config or {}
        url = cfg.get("url", "")
        if not url:
            return "up"
        try:
            response = httpx.get(
                url,
                timeout=float(cfg.get("timeout", 10)),
                follow_redirects=True,
            )
        except httpx.HTTPError:
            return "down"
        return "up" if 200 <= response.status_code < 400 else "down"

    def poll(self, source, state: dict):
        cfg = source.config or {}
        project = cfg.get("project") or source.team or source.name
        return self.parse(
            self.fetch(source, state),
            state,
            project=project,
            url=cfg.get("url", ""),
        )
