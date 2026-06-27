"""Advisor tick runner for public WhatsApp groups."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from argus.engine import EngineOutageError, run_agent
from argus.v2 import alerts
from argus.v2 import contracts
from argus.v2.advisor import state
from argus.v2.skills import registry as skills


EngineRunner = Callable[[str], str]
Sender = Callable[[str, str, str | None, str | None, str | None], bool]


def run(*, now: int | None = None, engine_runner: EngineRunner | None = None,
        sender: Sender | None = None, groups: list[str] | None = None) -> int:
    now = now or int(time.time())
    engine_runner = engine_runner or _run_engine
    sender = sender or _send
    groups = groups if groups is not None else _groups()
    if not groups or not _bot_ids():
        return 0
    processed = 0
    for jid in groups:
        processed += _process_group(jid, now, engine_runner, sender)
    return processed


def _process_group(jid: str, now: int, engine_runner: EngineRunner,
                   sender: Sender) -> int:
    rows = state.messages(jid)
    cur = state.cursor(jid)
    if cur >= len(rows):
        return 0
    coalesce = _cfg_int("COALESCE_SEC", 120)
    user_cap = _cfg_int("USER_HOURLY", 40)
    group_cap = _cfg_int("GROUP_HOURLY", 200)
    context_n = _cfg_int("CONTEXT_MESSAGES", 20)
    max_chars = _cfg_int("MAX_REPLY_CHARS", 700)
    max_parts = _cfg_int("MAX_REPLY_PARTS", 4)
    max_attempts = _cfg_int("MAX_ATTEMPTS", 3)
    processed = 0
    n = cur
    while n < len(rows):
        row = rows[n]
        ts = int(row.get("ts") or 0)
        if now - ts < coalesce:
            break
        if not _is_mention(jid, row, now):
            n += 1
            state.set_cursor(jid, n)
            processed += 1
            continue

        message_id = str(row.get("id") or "")
        participant = str(row.get("participant") or "")
        participant_jid = str(row.get("participant_jid") or "")
        trigger_body = str(row.get("body") or "")
        push = str(row.get("push_name") or "user")
        body, end = _merge_burst(rows, n, now, coalesce, participant)

        if state.replies_last_hour(jid, now) >= group_cap:
            _skip(jid, message_id, "rate_group", now)
            n = end
            state.set_cursor(jid, n)
            processed += 1
            continue
        if state.replies_last_hour(jid, now, participant) >= user_cap:
            _skip(jid, message_id, "rate_user", now)
            n = end
            state.set_cursor(jid, n)
            processed += 1
            continue

        abuse = (engine_runner(_contract("abuse") + "\n\nMessage:\n" + body) or "").strip()
        if not abuse or "UNSAFE" in abuse:
            _alert("warn", "advisor", f"advisor-abuse-{message_id}",
                   f"advisor: gated message from {participant} in {jid}")
            _skip(jid, message_id, "abuse", now)
            n = end
            state.set_cursor(jid, n)
            processed += 1
            continue

        context = "\n".join(
            f"{r.get('push_name') or 'user'}: {r.get('body') or ''}"
            for r in rows[max(0, len(rows) - context_n):]
        )
        prompt = _contract("advisor")
        identity = _contract("identity")
        if identity:
            prompt += "\n\nAbout the system you run on:\n" + identity
        prompt += f"\n\nRecent conversation:\n{context}\n\nAddressed message from {push}:\n{body}"
        skills_block = skills.block_for("advisor", body)
        if skills_block:
            prompt += "\n\n" + skills_block

        failure = ""
        sent = 0
        try:
            reply = (engine_runner(prompt) or "").strip()
        except Exception:
            reply = ""
            failure = "engine"
        if not reply:
            failure = failure or "engine"
        else:
            for idx, part in enumerate(split_reply(reply, max_chars, max_parts)):
                quoted = message_id if idx == 0 else None
                quoted_participant = participant_jid if idx == 0 else None
                quoted_body = trigger_body if idx == 0 else None
                if sender(jid, part, quoted, quoted_participant, quoted_body):
                    sent += 1
                    continue
                failure = "delivery"
                break
            if not failure:
                state.record_reply(jid, {
                    "ts": now, "participant": participant,
                    "reply_to_id": message_id, "parts": sent,
                })
                state.clear_attempts(jid, message_id)
            elif sent > 0:
                state.record_reply(jid, {
                    "ts": now, "participant": participant,
                    "reply_to_id": message_id, "parts": sent,
                })
                state.clear_attempts(jid, message_id)
                _skip(jid, message_id, "partial_delivery", now)
                n = end
                state.set_cursor(jid, n)
                processed += 1
                continue

        if failure:
            attempt_count = state.bump_attempts(jid, message_id)
            if attempt_count >= max_attempts:
                _alert("warn", "advisor", f"advisor-fail-{message_id}",
                       f"advisor: giving up on {message_id} in {jid} ({failure})")
                _skip(jid, message_id, failure, now)
                state.clear_attempts(jid, message_id)
                n = end
                state.set_cursor(jid, n)
                processed += 1
                continue
            break

        n = end
        state.set_cursor(jid, n)
        processed += 1
    return processed


def split_reply(text: str, max_chars: int, max_parts: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:max_chars])
            line = line[max_chars:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > max_chars:
            if current:
                parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return [part for part in parts if part][:max_parts]


def _merge_burst(rows: list[dict], index: int, now: int, coalesce: int,
                 participant: str) -> tuple[str, int]:
    body = str(rows[index].get("body") or "")
    end = index + 1
    while end < len(rows):
        row = rows[end]
        ts = int(row.get("ts") or 0)
        if now - ts < coalesce:
            break
        if str(row.get("participant") or "") != participant:
            break
        body += "\n" + str(row.get("body") or "")
        end += 1
    return body, end


def _is_mention(jid: str, row: dict, now: int) -> bool:
    body = str(row.get("body") or "")
    quoted = _digits(str(row.get("quoted_participant") or ""))
    for bot_id in _bot_ids():
        if bot_id in [_digits(str(x)) for x in row.get("mentioned") or []]:
            return True
        if quoted == bot_id:
            return True
        if len(bot_id) >= 6 and f"@{bot_id}" in body:
            return True
    participant = str(row.get("participant") or "")
    return state.active_conversation(jid, now, participant, _cfg_int("CONTINUITY_MIN", 10))


def _skip(jid: str, message_id: str, reason: str, now: int) -> None:
    state.record_reply(jid, {
        "ts": now, "reply_to_id": message_id, "skipped": True, "reason": reason,
    })


def _run_engine(prompt: str) -> str:
    engine = os.environ.get("ARGUS_ADVISOR_ENGINE") or "claude-code"
    fallback = os.environ.get("ARGUS_FALLBACK_ENGINE", "codex")
    with _tool_less_env():
        try:
            return run_agent(engine, prompt).text
        except EngineOutageError:
            if fallback and fallback != engine:
                return run_agent(fallback, prompt).text
            raise


@contextmanager
def _tool_less_env():
    previous = {key: os.environ.get(key) for key in [
        "ARGUS_CLAUDE_TOOLS", "ARGUS_CODEX_SANDBOX", "ARGUS_AGENT_CWD",
        "ARGUS_ENGINE_TIMEOUT",
    ]}
    with tempfile.TemporaryDirectory(prefix="argus-advisor-") as cwd:
        os.environ["ARGUS_CLAUDE_TOOLS"] = ""
        os.environ["ARGUS_CODEX_SANDBOX"] = "read-only"
        os.environ["ARGUS_AGENT_CWD"] = cwd
        os.environ.setdefault("ARGUS_ENGINE_TIMEOUT", os.environ.get("ARGUS_ADVISOR_ENGINE_TIMEOUT", "90"))
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _send(jid: str, text: str, quoted_id: str | None = None,
          quoted_participant: str | None = None, quoted_body: str | None = None) -> bool:
    import httpx

    base = os.environ.get("ARGUS_WA_URL", "http://127.0.0.1:8080").rstrip("/")
    instance = os.environ.get("ARGUS_WA_INSTANCE", "")
    apikey = os.environ.get("ARGUS_WA_APIKEY", "")
    if not apikey and os.environ.get("ARGUS_WA_APIKEY_FILE"):
        apikey = Path(os.environ["ARGUS_WA_APIKEY_FILE"]).read_text(encoding="utf-8").strip()
    if not instance or not apikey:
        return False
    payload: dict = {"number": jid, "text": text}
    if quoted_id:
        key = {"id": quoted_id, "fromMe": False, "remoteJid": jid}
        if quoted_participant:
            key["participant"] = quoted_participant
        payload["quoted"] = {"key": key, "message": {"conversation": quoted_body or ""}}
    try:
        response = httpx.post(
            f"{base}/message/sendText/{instance}",
            headers={"apikey": apikey},
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
    except Exception:
        return False
    return True


def _alert(severity: str, project: str, fingerprint: str, title: str) -> None:
    alerts.emit(
        severity=severity,
        project=project,
        fingerprint=fingerprint,
        message=title,
    )


def _contract(name: str) -> str:
    root = os.environ.get("ARGUS_ADVISOR_CONTRACT_DIR")
    if root:
        path = Path(root).expanduser() / f"{name}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
    return contracts.ADVISOR.get(name, "")


def _cfg_int(name: str, default: int) -> int:
    value = os.environ.get(f"ARGUS_ADVISOR_{name}", "")
    return int(value) if value.isdigit() else default


def _groups() -> list[str]:
    return [g.strip() for g in os.environ.get("ARGUS_ADVISOR_GROUPS", "").split(",") if g.strip()]


def _bot_ids() -> list[str]:
    return [_digits(x) for x in os.environ.get("ARGUS_ADVISOR_BOT_IDS", "").split(",") if _digits(x)]


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())
