"""Pure Python commitment reminder runner."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

from argus.engine import EngineOutageError
from argus.v2 import alerts
from argus.v2.context import engine, sanitize, source, state

Sender = Callable[[str, str], bool]


@dataclass(frozen=True)
class ReminderResult:
    delivered: int = 0
    extracted: int = 0
    no_due: bool = False
    failed: bool = False
    extraction_outage: bool = False


def run(*, now_iso: str | None = None, engine_runner: engine.EngineRunner | None = None,
        sender: Sender | None = None) -> ReminderResult:
    now_iso = now_iso or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sender = sender or _send
    ex = _extract_batch(engine_runner=engine_runner)
    if ex == "outage":
        _alert("context-remind-outage", "remind: extraction engine outage")
    elif ex == "failed":
        _alert("context-remind-extract", "remind: extraction failed")

    window = _env_int("ARGUS_CONTEXT_SURFACE_HOURS", 48)
    due_rows = [
        row for row in state.commit_list("open")
        if should_surface(str(row.get("due_at") or ""),
                          str(row.get("snooze_until") or ""),
                          now_iso,
                          window)
    ]
    if not due_rows:
        return ReminderResult(extracted=0, no_due=True, extraction_outage=(ex == "outage"))

    to = os.environ.get("ARGUS_CONTEXT_WA_TO", "")
    if not to:
        _alert("context-remind-noto", "remind: ARGUS_CONTEXT_WA_TO unset, cannot deliver")
        return ReminderResult(failed=True, extraction_outage=(ex == "outage"))

    fingerprint = _batch_fingerprint([str(row["id"]) for row in due_rows])
    if state.batch_seen(fingerprint):
        return ReminderResult(no_due=True, extraction_outage=(ex == "outage"))

    text = format_reminder(due_rows)
    if not text:
        return ReminderResult(no_due=True, extraction_outage=(ex == "outage"))

    state.batch_record(fingerprint)
    if not sender(to, text):
        _alert("context-remind-send", "remind: WhatsApp DM failed")
        return ReminderResult(failed=True, extraction_outage=(ex == "outage"))

    for row in due_rows:
        commit_id = str(row["id"])
        state.commit_set(commit_id, "status", "surfaced")
        state.commit_set(commit_id, "surfaced_at", now_iso)
    return ReminderResult(delivered=len(due_rows), extraction_outage=(ex == "outage"))


def _extract_batch(*, engine_runner: engine.EngineRunner | None = None) -> str:
    driver = os.environ.get("ARGUS_CONTEXT_SOURCE", "sqlite")
    limit = _env_int("ARGUS_CONTEXT_BATCH_LIMIT", 200)
    since = state.get_watermark("remind")
    try:
        rows = source.fetch(driver, since, limit)
    except source.SourceError:
        return "failed"
    if not rows:
        return "ok"

    max_id = since
    for row in rows:
        clean = sanitize.sanitize(row.body)
        if not sanitize.looks_like_commitment(clean):
            if row.id > max_id:
                max_id = row.id
            continue
        try:
            raw = engine.call("commitment", clean, engine_runner=engine_runner)
        except EngineOutageError:
            if max_id > since:
                state.set_watermark("remind", driver, max_id)
            return "outage"
        obj = _parse_commitment(raw)
        if not obj:
            if row.id > max_id:
                max_id = row.id
            continue
        try:
            state.commit_upsert(
                str(obj.get("who") or ""),
                str(obj.get("what") or ""),
                _none_to_empty(obj.get("due_at")),
                f"{driver}:{row.id}",
            )
        except Exception:
            if max_id > since:
                state.set_watermark("remind", driver, max_id)
            return "failed"
        if row.id > max_id:
            max_id = row.id
    if max_id > since:
        state.set_watermark("remind", driver, max_id)
    return "ok"


def _parse_commitment(raw: str) -> dict | None:
    parsed = _json_obj(raw or "")
    if not parsed:
        match = re.search(r"\{.*\}", raw or "", flags=re.S)
        parsed = _json_obj(match.group(0)) if match else None
    if not parsed:
        return None
    what = parsed.get("what")
    if not isinstance(what, str) or not what.strip():
        return None
    return parsed


def _json_obj(raw: str) -> dict | None:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def should_surface(due_at: str, snooze_until: str, now_iso: str, window_hours: int) -> bool:
    now = _parse_iso(now_iso)
    if now is None:
        return False
    if snooze_until:
        snooze = _parse_iso(snooze_until)
        if snooze is not None and snooze > now:
            return False
    if not due_at:
        return False
    due = _parse_iso(f"{due_at}T23:59:59Z" if len(due_at) <= 10 else due_at)
    if due is None:
        return False
    return due <= now + timedelta(hours=window_hours)


def format_reminder(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        what = str(row.get("what") or "").strip()
        if not what:
            continue
        due = str(row.get("due_at") or "").strip()
        suffix = f" (by {due})" if due else ""
        lines.append(f"- {what}{suffix}")
    if not lines:
        return ""
    return "Reminder - you committed to:\n" + "\n".join(lines)


def _send(to: str, text: str) -> bool:
    import httpx

    base = os.environ.get("ARGUS_WA_URL", "http://127.0.0.1:8080").rstrip("/")
    instance = os.environ.get("ARGUS_WA_INSTANCE", "")
    apikey = os.environ.get("ARGUS_WA_APIKEY", "")
    if not apikey and os.environ.get("ARGUS_WA_APIKEY_FILE"):
        apikey = Path(os.path.expandvars(os.environ["ARGUS_WA_APIKEY_FILE"])).expanduser().read_text(
            encoding="utf-8"
        ).strip()
    if not instance or not apikey:
        return False
    try:
        response = httpx.post(
            f"{base}/message/sendText/{instance}",
            headers={"apikey": apikey},
            json={"number": to, "text": text},
            timeout=20,
        )
        response.raise_for_status()
    except Exception:
        return False
    return True


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _batch_fingerprint(ids: list[str]) -> str:
    payload = ",".join(sorted(item for item in ids if item))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _none_to_empty(value) -> str:
    if value is None or value == "null":
        return ""
    return str(value)


def _alert(fingerprint: str, message: str) -> None:
    alerts.emit(
        severity="error",
        project="context",
        fingerprint=fingerprint,
        message=message,
        channel="whatsapp",
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default
