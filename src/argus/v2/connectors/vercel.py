"""Vercel connectors.

`vercel` polls deployment failures. `vercel_events` polls finite deployment
events for production 5xx responses. Both can reuse local Vercel CLI auth when
`secret_ref` is omitted.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from argus.v2.connectors.base import Signal, register

_VERCEL_TOKEN_ENDPOINT = "https://api.vercel.com/login/oauth/token"
_VERCEL_CLI_CLIENT_ID = "cl_HYyOPBNtFMfHhaUn9L4QPfTZz6TP47bp"


def _default_auth_file() -> Path:
    override = os.environ.get("VERCEL_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    app_support = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"
    if app_support.exists():
        return app_support
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "com.vercel.cli" / "auth.json"


def _cli_auth_token(path: Path) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("token")
    expires_at = int(data.get("expiresAt") or 0)
    refresh_token = data.get("refreshToken")
    if token and (not expires_at or expires_at > int(time.time()) + 60):
        return str(token)
    if not refresh_token:
        return str(token) if token else None

    import httpx
    response = httpx.post(
        _VERCEL_TOKEN_ENDPOINT,
        data={
            "client_id": _VERCEL_CLI_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": str(refresh_token),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    response.raise_for_status()
    refreshed = response.json()
    token = refreshed["access_token"]
    data["token"] = token
    data["expiresAt"] = int(time.time()) + int(refreshed.get("expires_in", 0))
    if refreshed.get("refresh_token"):
        data["refreshToken"] = refreshed["refresh_token"]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return str(token)


def _auth_token(source) -> str:
    if source.secret:
        return source.secret
    cfg = source.config or {}
    auth_file = Path(cfg.get("auth_file") or _default_auth_file()).expanduser()
    token = _cli_auth_token(auth_file)
    if not token:
        raise RuntimeError("Vercel token missing; set secret_ref or run vercel login")
    return token


@register
class VercelConnector:
    type = "vercel"

    @staticmethod
    def parse(raw, state: dict, *, states=("ERROR",)):
        last = int(state.get("last_created", 0))
        items = raw.get("deployments", []) if isinstance(raw, dict) else (raw or [])
        signals, newest = [], last
        for d in items:
            created = int(d.get("createdAt") or d.get("created") or 0)
            if d.get("state") not in states or created <= last:
                continue
            signals.append(Signal(
                fingerprint=str(d.get("uid") or d.get("id")),
                payload={"name": d.get("name"), "state": d.get("state"),
                         "url": d.get("url"), "created": created}))
            newest = max(newest, created)
        return signals, {"last_created": newest}

    def fetch(self, source, state: dict):  # pragma: no cover
        import httpx
        cfg = source.config or {}
        params = {"limit": cfg.get("limit", 20)}
        if cfg.get("project"):
            params["projectId"] = cfg["project"]
        if cfg.get("team"):
            params["teamId"] = cfg["team"]
        token = _auth_token(source)
        r = httpx.get("https://api.vercel.com/v6/deployments",
                      headers={"Authorization": f"Bearer {token}"},
                      params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def poll(self, source, state: dict):
        states = tuple((source.config or {}).get("states", ["ERROR"]))
        return self.parse(self.fetch(source, state), state, states=states)


def _normalize_ms(value) -> int:
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if ts and ts < 10_000_000_000:
        return ts * 1000
    return ts


def _event_items(raw) -> list[dict]:
    if isinstance(raw, dict):
        events = raw.get("events")
        if isinstance(events, list):
            return [e for e in events if isinstance(e, dict)]
        deployments = raw.get("deployments")
        if isinstance(deployments, list):
            return [e for e in deployments if isinstance(e, dict)]
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def _event_created(event: dict) -> int:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
    for value in (
        event.get("created"),
        payload.get("date"),
        payload.get("created"),
        proxy.get("timestamp"),
    ):
        ts = _normalize_ms(value)
        if ts:
            return ts
    return 0


def _event_status(event: dict) -> int:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
    for value in (
        payload.get("statusCode"),
        payload.get("responseStatusCode"),
        proxy.get("statusCode"),
        proxy.get("responseStatusCode"),
    ):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _event_fingerprint(event: dict, *, deployment: str) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
    for value in (
        payload.get("id"),
        payload.get("requestId"),
        event.get("id"),
        payload.get("serial"),
    ):
        if value:
            return f"vercel-event-{deployment}-{value}"
    created = _event_created(event)
    path = proxy.get("path") or payload.get("path") or ""
    return f"vercel-event-{deployment}-{created}-{_event_status(event)}-{path}"


def _event_payload(event: dict, *, deployment: str) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
    status = _event_status(event)
    host = proxy.get("host") or payload.get("host") or ""
    path = proxy.get("path") or payload.get("path") or ""
    message = f"Vercel {status} response"
    if path:
        message = f"{message} on {path}"
    return {
        "source": "vercel",
        "severity": "error" if status >= 500 else "warn",
        "fingerprint": _event_fingerprint(event, deployment=deployment),
        "message": message,
        "kind": "runtime",
        "status_code": status,
        "deployment_id": payload.get("deploymentId") or deployment,
        "request_id": payload.get("requestId") or "",
        "method": proxy.get("method") or payload.get("method") or "",
        "host": host,
        "path": path,
        "url": f"https://{host}{path}" if host and path else "",
        "created": _event_created(event),
    }


@register
class VercelEventsConnector:
    type = "vercel_events"

    @staticmethod
    def parse(raw, state: dict, *, min_status: int = 500, deployment: str = ""):
        last = int(state.get("last_created", 0))
        if isinstance(raw, dict):
            newest = max(last, _normalize_ms(raw.get("_argus_since_ms")))
        else:
            newest = last
        signals = []
        for event in _event_items(raw):
            created = _event_created(event)
            status = _event_status(event)
            if created <= last or status < min_status:
                continue
            fingerprint = _event_fingerprint(event, deployment=deployment)
            signals.append(Signal(
                fingerprint=fingerprint,
                payload=_event_payload(event, deployment=deployment),
            ))
            newest = max(newest, created)
        return signals, {"last_created": newest}

    def _latest_deployment(self, source, token: str) -> str:
        import httpx
        cfg = source.config or {}
        project = cfg.get("project")
        if not project:
            raise RuntimeError("Vercel events connector requires config.project or config.deployment")
        params = {
            "projectId": project,
            "limit": cfg.get("deployment_limit", 1),
            "target": cfg.get("target", "production"),
            "state": cfg.get("state", "READY"),
        }
        if cfg.get("team"):
            params["teamId"] = cfg["team"]
        r = httpx.get("https://api.vercel.com/v6/deployments",
                      headers={"Authorization": f"Bearer {token}"},
                      params=params, timeout=20)
        r.raise_for_status()
        deployments = r.json().get("deployments", [])
        if not deployments:
            raise RuntimeError("Vercel events connector found no READY production deployment")
        deployment = deployments[0]
        return str(deployment.get("uid") or deployment.get("id") or deployment.get("url"))

    def fetch(self, source, state: dict):  # pragma: no cover
        import httpx
        cfg = source.config or {}
        token = _auth_token(source)
        deployment = str(cfg.get("deployment") or self._latest_deployment(source, token))
        lookback_seconds = int(cfg.get("lookback_seconds", 600))
        floor = int(time.time() * 1000) - (lookback_seconds * 1000)
        since = max(int(state.get("last_created", 0)), floor)
        params = {
            "follow": 0,
            "limit": cfg.get("limit", 100),
            "since": since,
            "statusCode": cfg.get("status_code", "5xx"),
        }
        if cfg.get("team"):
            params["teamId"] = cfg["team"]
        r = httpx.get(f"https://api.vercel.com/v3/deployments/{deployment}/events",
                      headers={"Authorization": f"Bearer {token}"},
                      params=params, timeout=20)
        r.raise_for_status()
        return {"events": r.json(), "_argus_since_ms": since, "_argus_deployment": deployment}

    def poll(self, source, state: dict):
        raw = self.fetch(source, state)
        deployment = str((source.config or {}).get("deployment") or raw.get("_argus_deployment") or "")
        min_status = int((source.config or {}).get("min_status", 500))
        return self.parse(raw, state, min_status=min_status, deployment=deployment)
