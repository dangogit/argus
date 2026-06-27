"""Firestore REST connector for Firebase-backed bug collections."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

from argus.v2.connectors.base import Signal, register

_SEEN_CAP = 1000
_FIREBASE_TOKEN_ENDPOINT = "https://www.googleapis.com/oauth2/v3/token"
_FIREBASE_CLI_CLIENT_ID = "563584335869-fgrhgmd47bqnekij5i8b5pr03ho849e6.apps.googleusercontent.com"
_FIREBASE_CLI_CLIENT_SECRET = "j9iVZfS8kkCEFUPaAeJV0sAi"


def _default_auth_file() -> Path:
    override = os.environ.get("FIREBASE_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "configstore" / "firebase-tools.json"


def _cli_auth_token(path: Path) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    expires_at = int(tokens.get("expires_at") or 0)
    if token and (not expires_at or expires_at > int(time.time() * 1000) + 60000):
        return str(token)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return str(token) if token else None

    import httpx
    response = httpx.post(
        _FIREBASE_TOKEN_ENDPOINT,
        data={
            "refresh_token": str(refresh_token),
            "client_id": _FIREBASE_CLI_CLIENT_ID,
            "client_secret": _FIREBASE_CLI_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    response.raise_for_status()
    refreshed = response.json()
    token = refreshed["access_token"]
    tokens["access_token"] = token
    tokens["expires_at"] = int(time.time() * 1000) + int(refreshed.get("expires_in", 0)) * 1000
    if refreshed.get("refresh_token"):
        tokens["refresh_token"] = refreshed["refresh_token"]
    data["tokens"] = tokens
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return str(token)


def _auth_token(source) -> str:
    if source.secret:
        return source.secret
    cfg = source.config or {}
    auth_file = Path(cfg.get("auth_file") or _default_auth_file()).expanduser()
    token = _cli_auth_token(auth_file)
    if not token:
        raise RuntimeError("Firebase token missing; set secret_ref or run firebase login")
    return token


def _unwrap(value):
    if not isinstance(value, dict):
        return ""
    for key in (
        "stringValue",
        "integerValue",
        "doubleValue",
        "booleanValue",
        "timestampValue",
    ):
        if key in value:
            return value[key]
    if "mapValue" in value:
        return value["mapValue"]
    if "arrayValue" in value:
        return value["arrayValue"]
    return ""


def _severity(value) -> str:
    s = str(value or "").lower()
    if s in ("critical", "fatal"):
        return "critical"
    if s in ("error", "high"):
        return "error"
    if s in ("warn", "warning", "medium"):
        return "warn"
    if s in ("info", "low"):
        return "info"
    return "warn"


def _doc_id(name: str) -> str:
    return (name or "").rstrip("/").split("/")[-1] or "unknown"


@register
class FirebaseConnector:
    type = "firebase"

    @staticmethod
    def parse(raw, state: dict, *, project: str, collection: str = "bug_reports",
              title_column: str = "title", severity_column: str | None = None):
        seen = set(state.get("seen", []))
        next_seen = list(state.get("seen", []))
        docs = raw.get("documents", []) if isinstance(raw, dict) else []
        signals: list[Signal] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            docid = _doc_id(str(doc.get("name", "")))
            fields = {
                key: _unwrap(value)
                for key, value in (doc.get("fields") or {}).items()
            }
            fingerprint = f"firebase-{project}-{collection}-{docid}"
            if fingerprint in seen:
                continue
            title = (
                fields.get(title_column)
                or fields.get("description")
                or fields.get("message")
                or fields.get("title")
                or f"doc {docid}"
            )
            finding = {
                "source": "firebase",
                "severity": _severity(fields.get(severity_column)) if severity_column else "warn",
                "fingerprint": fingerprint,
                "message": str(title),
                "url": (
                    f"https://console.firebase.google.com/project/{project}"
                    f"/firestore/data/{collection}/{docid}"
                    if project else ""
                ),
                "kind": "bug",
                "last_seen": (
                    fields.get("created_at")
                    or fields.get("updated_at")
                    or fields.get("createdAt")
                    or ""
                ),
                "fields": fields,
            }
            signals.append(Signal(fingerprint=fingerprint, payload=finding))
            seen.add(fingerprint)
            next_seen.append(fingerprint)
        return signals, {"seen": next_seen[-_SEEN_CAP:]}

    def fetch(self, source, state: dict):  # pragma: no cover
        import httpx

        cfg = source.config or {}
        project = cfg.get("project", "")
        if not project:
            return {"documents": []}
        token = _auth_token(source)
        database = cfg.get("database", "(default)")
        collection = cfg.get("collection", "bug_reports")
        limit = int(cfg.get("limit", 25))
        url = (
            "https://firestore.googleapis.com/v1/projects/"
            f"{quote(project, safe='')}/databases/{quote(database, safe='')}"
            f"/documents/{quote(collection, safe='/')}?pageSize={limit}"
        )
        r = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=float(cfg.get("timeout", 15)),
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {"documents": []}

    def poll(self, source, state: dict):
        cfg = source.config or {}
        collection = cfg.get("collection", "bug_reports")
        return self.parse(
            self.fetch(source, state),
            state,
            project=cfg.get("project") or source.team or source.name,
            collection=collection,
            title_column=cfg.get("title_column", "title"),
            severity_column=cfg.get("severity_column"),
        )
