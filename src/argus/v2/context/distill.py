"""Pure Python context distill runner."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from argus.engine import EngineOutageError
from argus.v2 import alerts
from argus.v2.context import engine, sanitize, source, state, vault


@dataclass(frozen=True)
class DistillResult:
    watermark: int
    processed: int
    no_new: bool = False
    outage: bool = False


def run(*, engine_runner: engine.EngineRunner | None = None) -> DistillResult:
    driver = os.environ.get("ARGUS_CONTEXT_SOURCE", "sqlite")
    vault_path = os.environ.get("ARGUS_CONTEXT_VAULT", "")
    if not vault_path:
        _alert("context-distill-ready", "distill: ARGUS_CONTEXT_VAULT required")
        raise RuntimeError("ARGUS_CONTEXT_VAULT required")
    limit = _env_int("ARGUS_CONTEXT_BATCH_LIMIT", 200)
    checkpoint_every = max(1, _env_int("ARGUS_CONTEXT_CHECKPOINT_EVERY", 25))
    since = state.get_watermark("distill")
    try:
        rows = source.fetch(driver, since, limit)
    except source.SourceError as exc:
        _alert("context-distill-fetch", f"distill: source fetch failed ({exc})")
        raise
    if not rows:
        return DistillResult(watermark=since, processed=0, no_new=True)

    max_id = since
    since_checkpoint = 0
    processed = 0

    def checkpoint() -> None:
        if max_id > since:
            state.set_watermark("distill", driver, max_id)

    for row in rows:
        clean = sanitize.sanitize(row.body)
        if not clean:
            if row.id > max_id:
                max_id = row.id
            processed += 1
            since_checkpoint += 1
            if since_checkpoint >= checkpoint_every:
                checkpoint()
                since_checkpoint = 0
            continue

        try:
            raw = engine.call("distill", clean, engine_runner=engine_runner)
        except EngineOutageError:
            _alert("context-distill-outage",
                   f"distill: engine outage, batch stopped at watermark {max_id}")
            checkpoint()
            return DistillResult(watermark=max_id, processed=processed, outage=True)

        project, facts = _parse_distill(raw)
        project = engine.resolve_project(project)
        facts_ok = True
        iso = _date_prefix(row.ts)
        for fact in facts:
            key = str(fact.get("key") or "")
            value = str(fact.get("value") or "")
            if not key or not value:
                continue
            try:
                status = vault.apply_fact(vault_path, project, key, value, iso)
            except Exception:
                status = "error"
            if status in {"deferred", "error"}:
                facts_ok = False
        if not facts_ok:
            break

        if row.id > max_id:
            max_id = row.id
        processed += 1
        since_checkpoint += 1
        if since_checkpoint >= checkpoint_every:
            checkpoint()
            since_checkpoint = 0

    checkpoint()
    return DistillResult(watermark=max_id, processed=processed)


def _parse_distill(raw: str) -> tuple[str, list[dict]]:
    try:
        parsed = json.loads(raw or "")
    except json.JSONDecodeError:
        return "general", []
    if not isinstance(parsed, dict):
        return "general", []
    facts = parsed.get("facts")
    if not isinstance(facts, list):
        facts = []
    return str(parsed.get("project") or "general"), [f for f in facts if isinstance(f, dict)]


def _date_prefix(value: str) -> str:
    if value and len(value) >= 10 and value[:10].count("-") == 2:
        return value[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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
