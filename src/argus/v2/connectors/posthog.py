"""PostHog connector for fired alerts or active error-tracking issues."""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register

_SEEN_CAP = 1000


@register
class PostHogConnector:
    type = "posthog"

    @staticmethod
    def parse(raw, state: dict):
        last = state.get("last_created") or ""
        items = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
        signals, newest = [], last
        for it in items:
            if it.get("activity") != "alert_fired":
                continue
            created = it.get("created_at", "")
            if created <= last:
                continue
            detail = it.get("detail", {}) or {}
            signals.append(Signal(fingerprint=str(it["id"]),
                                  payload={"name": detail.get("name"),
                                           "value": detail.get("value")}))
            newest = max(newest, created)
        return signals, {"last_created": newest}

    @staticmethod
    def parse_issues(raw, state: dict, *, project: str, host: str = "",
                     project_id: str = ""):
        seen = set(state.get("seen", []))
        next_seen = list(state.get("seen", []))
        items = raw.get("results", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        signals: list[Signal] = []
        for issue in items:
            if not isinstance(issue, dict):
                continue
            if issue.get("status", "active") != "active":
                continue
            issue_id = str(issue.get("id", "unknown"))
            fingerprint = f"posthog-{project}-{issue_id}"
            if fingerprint in seen:
                continue
            message = issue.get("name") or issue.get("title") or issue.get("description") or "issue"
            url = f"{host.rstrip('/')}/project/{project_id}/error_tracking/{issue_id}" if host and project_id else ""
            payload = {
                "source": "posthog",
                "severity": "warn",
                "fingerprint": fingerprint,
                "message": str(message),
                "url": url,
                "kind": "error",
                "count": issue.get("occurrences") or issue.get("count") or 0,
                "last_seen": issue.get("last_seen") or "",
                "issue": issue,
            }
            signals.append(Signal(fingerprint=fingerprint, payload=payload))
            seen.add(fingerprint)
            next_seen.append(fingerprint)
        return signals, {"seen": next_seen[-_SEEN_CAP:]}

    def fetch(self, source, state: dict):
        # NETWORK -- not gate-run.
        from argus.v2.connectors.client import fetch_json
        cfg = source.config or {}
        base = cfg.get("host", "https://us.posthog.com")
        if cfg.get("endpoint") == "error_tracking":
            limit = int(cfg.get("limit", 25))
            url = f"{base}/api/projects/{cfg['project']}/error_tracking/issues/?limit={limit}&order_by=last_seen"
        else:
            url = f"{base}/api/projects/{cfg['project']}/activity_log/"
        return fetch_json(url, headers={"Authorization": f"Bearer {source.secret}"})

    def poll(self, source, state: dict):
        cfg = source.config or {}
        raw = self.fetch(source, state)
        if cfg.get("endpoint") == "error_tracking":
            return self.parse_issues(
                raw,
                state,
                project=cfg.get("project_name") or source.team or source.name,
                host=cfg.get("host", "https://us.posthog.com"),
                project_id=str(cfg.get("project", "")),
            )
        return self.parse(raw, state)
