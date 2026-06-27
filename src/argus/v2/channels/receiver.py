"""Inbound webhook receiver. handle_webhook (gate-tested) validates + ingests;
serve() (seam) is the loopback HTTP socket server."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time

from argus.v2.channels import router


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    count: int
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"


def has_inbound_auth(cfg) -> bool:
    return bool(
        getattr(cfg.company.defaults, "webhook_secret", None)
        or _slack_signing_secrets(cfg)
    )


def handle_webhook(conn, cfg, channel_type: str, body: bytes, headers: dict):
    res = response_for_webhook(conn, cfg, channel_type, body, headers)
    return res.status, res.count


def response_for_webhook(
    conn,
    cfg,
    channel_type: str,
    body: bytes,
    headers: dict,
) -> WebhookResponse:
    headers = _normalize_headers(headers)
    if channel_type not in router.REGISTRY:
        return WebhookResponse(400, 0)
    if not _authorized(cfg, channel_type, body, headers):
        return WebhookResponse(401, 0)
    try:
        raw = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return WebhookResponse(400, 0)
    if channel_type == "slack" and isinstance(raw, dict) and raw.get("type") == "url_verification":
        return WebhookResponse(200, 0, str(raw.get("challenge", "")).encode("utf-8"))
    n = router.inbound(conn, cfg, channel_type, raw, _shared_secret(cfg))
    conn.commit()
    return WebhookResponse(200, n)


def _normalize_headers(headers: dict | None) -> dict:
    # HTTP header names are case-insensitive; serve() hands us a plain dict whose
    # .get is not, and Evolution sends a title-cased header.
    return {str(k).lower(): v for k, v in (headers or {}).items()}


def _shared_secret(cfg):
    secret = getattr(cfg.company.defaults, "webhook_secret", None)
    return secret


def _authorized(cfg, channel_type: str, body: bytes, headers: dict) -> bool:
    secret = _shared_secret(cfg)
    got = (headers.get("x-argus-secret") or headers.get("apikey")
           or headers.get("x-argus-token"))  # Evolution posts the secret as x-argus-token
    if secret and got == secret:
        return True
    if channel_type == "slack" and _valid_slack_signature(cfg, body, headers):
        return True
    return False


def _slack_signing_secrets(cfg) -> list[str]:
    secrets = []
    for team in cfg.teams:
        for channel in team.channels:
            if channel.type == "slack":
                signing_secret = (channel.config or {}).get("signing_secret")
                if signing_secret:
                    secrets.append(str(signing_secret))
    return secrets


def _valid_slack_signature(cfg, body: bytes, headers: dict) -> bool:
    timestamp = str(headers.get("x-slack-request-timestamp") or "")
    signature = str(headers.get("x-slack-signature") or "")
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > 300:
        return False

    base = b"v0:" + timestamp.encode("utf-8") + b":" + (body or b"")
    for signing_secret in _slack_signing_secrets(cfg):
        digest = hmac.new(
            signing_secret.encode("utf-8"),
            base,
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(f"v0={digest}", signature):
            return True
    return False


def serve(cfg, host: str = "127.0.0.1", port: int = 8787):  # pragma: no cover
    # SEAM: loopback HTTP server. Evolution, Telegram, and Slack webhooks POST here.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from argus.v2.db import pool

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            ctype = self.path.strip("/").rsplit("/", 1)[-1] or "whatsapp"
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            conn = pool.connect()
            try:
                res = response_for_webhook(conn, cfg, ctype, body, dict(self.headers))
            finally:
                conn.close()
            self.send_response(res.status)
            if res.body:
                self.send_header("Content-Type", res.content_type)
                self.send_header("Content-Length", str(len(res.body)))
            self.end_headers()
            if res.body:
                self.wfile.write(res.body)

    HTTPServer((host, port), H).serve_forever()
