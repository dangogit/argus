"""GitHub issues connector using the REST API."""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register

_SEEN_CAP = 1000


@register
class GitHubConnector:
    type = "github"

    @staticmethod
    def parse(raw, state: dict, *, project: str):
        seen = set(state.get("seen", []))
        next_seen = list(state.get("seen", []))
        issues = raw if isinstance(raw, list) else []
        signals: list[Signal] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("pull_request"):
                continue
            if str(issue.get("state", "open")).lower() != "open":
                continue
            number = issue.get("number")
            if number is None:
                continue
            fingerprint = f"github-{project}-{number}"
            if fingerprint in seen:
                continue
            title = str(issue.get("title") or f"issue #{number}")
            body = str(issue.get("body") or "")
            message = f"open issue #{number}: {title}"
            if body:
                message = f"{message}\n\n{body}"
            payload = {
                "source": "github",
                "severity": "warn",
                "fingerprint": fingerprint,
                "message": message,
                "url": issue.get("html_url") or "",
                "kind": "issue",
                "number": number,
                "title": title,
                "body": body,
            }
            signals.append(Signal(fingerprint=fingerprint, payload=payload))
            seen.add(fingerprint)
            next_seen.append(fingerprint)
        return signals, {"seen": next_seen[-_SEEN_CAP:]}

    def fetch(self, source, state: dict):  # pragma: no cover
        import httpx

        cfg = source.config or {}
        repo = cfg.get("repo", "")
        token = source.secret
        if not repo or not token:
            return []
        labels = cfg.get("labels")
        if isinstance(labels, str):
            labels = [labels]
        if not labels:
            # Open GitHub issues are NOT auto-work. Argus only picks up issues the
            # owner explicitly labels for it (config `labels: [argus]`); with no
            # `labels` set this connector is a deliberate no-op.
            return []
        limit = int(cfg.get("limit", 20))
        url = f"https://api.github.com/repos/{repo}/issues"
        r = httpx.get(
            url,
            params={"state": "open", "per_page": limit,
                    "labels": ",".join(str(x) for x in labels)},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=float(cfg.get("timeout", 15)),
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def poll(self, source, state: dict):
        cfg = source.config or {}
        project = cfg.get("project") or source.team or source.name
        return self.parse(self.fetch(source, state), state, project=project)
