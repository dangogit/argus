"""Generic config-driven HTTP/JSON connector. One connector instead of a new
hand-coded module per simple REST source: point it at a URL (or resolve one from
an OpenAPI operation) and map the JSON response to signals with dotted paths.

config (source.config):
  url: "https://api.x/v1/incidents"        # explicit endpoint, OR:
  spec_url: "https://api.x/openapi.json"   #   resolve the URL from an OpenAPI
  operation_id: "listIncidents"            #   operationId (server[0].url + path)
  method: GET
  headers: { Accept: application/json }
  auth_header: Authorization               # source.secret sent here as "Bearer <secret>"
  items_path: "data"                       # dotted path to the array (omit => whole body)
  id_path: "id"                            # dotted path to each item's id (fingerprint)
  cursor_path: "updated_at"                # optional high-water-mark; emit only items > last
  severity: info|warning|error             # tag on emitted signals (default info)
  message_path: "title"                    # optional dotted path for the signal message

Re-emitting an already-seen item is safe: the driver dedups on fingerprint at
ingest. `cursor_path` is an efficiency high-water-mark on top of that, not the
correctness boundary.
"""
from __future__ import annotations

import hashlib
import json

from argus.v2.connectors.base import Signal, register


def _dig(obj, path):
    """Walk a dotted path through nested dicts; None if any hop is missing."""
    if not path:
        return obj
    for key in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


@register
class OpenAPIConnector:
    type = "http"

    @staticmethod
    def parse(body, state, *, cfg, prefix):
        items = _dig(body, cfg.get("items_path", ""))
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return [], dict(state or {})
        cursor_path = cfg.get("cursor_path")
        last = (state or {}).get("cursor")
        new_last = last
        id_path = cfg.get("id_path", "id")
        msg_path = cfg.get("message_path")
        severity = cfg.get("severity", "info")
        signals = []
        for item in items:
            if cursor_path is not None:
                cv = _dig(item, cursor_path) if isinstance(item, dict) else None
                # ponytail: lexicographic compare -- correct for ISO timestamps and
                # fixed-width ids; for variable-width numeric ids the driver's
                # fingerprint dedup is still the backstop.
                if last is not None and cv is not None and str(cv) <= str(last):
                    continue
                if cv is not None and (new_last is None or str(cv) > str(new_last)):
                    new_last = cv
            ident = _dig(item, id_path) if isinstance(item, dict) else None
            if ident is None:
                ident = hashlib.sha256(
                    json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:16]
            fp = f"{prefix}-{ident}"
            message = _dig(item, msg_path) if (msg_path and isinstance(item, dict)) else None
            signals.append(Signal(fingerprint=fp, payload={
                "source": prefix, "severity": severity, "fingerprint": fp,
                "message": str(message) if message is not None else fp,
                "kind": "signal", "data": item,
            }))
        cursor = dict(state or {})
        if cursor_path is not None and new_last is not None:
            cursor["cursor"] = new_last
        return signals, cursor

    @staticmethod
    def url_for_operation(spec, operation_id):
        """Resolve an operationId to a full URL: first server url + the path it's
        declared under. "" when not found."""
        servers = spec.get("servers") or []
        base = (servers[0].get("url") if servers and isinstance(servers[0], dict) else "") or ""
        for path, ops in (spec.get("paths") or {}).items():
            if not isinstance(ops, dict):
                continue
            for op in ops.values():
                if isinstance(op, dict) and op.get("operationId") == operation_id:
                    return base.rstrip("/") + path
        return ""

    def _resolve_spec_url(self, cfg):  # pragma: no cover
        import httpx
        spec_url, op = cfg.get("spec_url"), cfg.get("operation_id")
        if not (spec_url and op):
            return ""
        try:
            spec = httpx.get(spec_url, timeout=15, follow_redirects=True).json()
        except (httpx.HTTPError, ValueError):
            return ""
        return self.url_for_operation(spec, op)

    def fetch(self, source, state):  # pragma: no cover
        import httpx
        cfg = source.config or {}
        url = cfg.get("url") or self._resolve_spec_url(cfg)
        if not url:
            return {}
        headers = dict(cfg.get("headers") or {})
        auth_header = cfg.get("auth_header")
        if auth_header and getattr(source, "secret", None):
            headers[auth_header] = f"Bearer {source.secret}"
        try:
            r = httpx.request(cfg.get("method", "GET"), url, headers=headers,
                              timeout=float(cfg.get("timeout", 15)), follow_redirects=True)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            return {}

    def poll(self, source, state):
        cfg = source.config or {}
        prefix = cfg.get("fingerprint_prefix") or getattr(source, "name", None) or "http"
        return self.parse(self.fetch(source, state), state, cfg=cfg, prefix=prefix)
