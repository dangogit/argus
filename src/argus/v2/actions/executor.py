"""Drain proposed actions under each team's autonomy policy. Reversible/internal
actions execute idempotently now; irreversible/outward actions pause at
awaiting_approval with an approvals record (notify the control channel). Any
unclassified risk already defaulted to irreversible_outward upstream."""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Json

from argus.v2.actions import handlers as _handlers
from argus.v2.config.schema import Autonomy

log = logging.getLogger(__name__)

_REAL = {
    "open_pr", "ready_pr", "merge_pr", "deploy", "close_pr", "comment_pr",
    "reopen_pr",
    "calendar_list", "calendar_get", "calendar_create", "calendar_update",
    "calendar_delete", "email_list", "email_search", "email_read", "email_reply",
    "email_archive", "email_draft", "content_queue", "social_publish",
    "bug_writeback",
    "set_user_balance",
}

# Server-side risk classification. The model's self-declared risk is ignored for
# converse-emitted actions; executor.risk_for is applied instead so the model can
# never mark an irreversible operation as reversible to auto-run it.
_REVERSIBLE = frozenset({
    "open_pr", "ready_pr", "close_pr", "comment_pr", "reopen_pr", "remember",
    "reply", "notify", "status",
    "calendar_list", "calendar_get", "email_list", "email_search", "email_read", "email_draft",
    "bug_writeback",
    "set_user_balance",
})
_PERSONAL_OUTWARD = frozenset({
    "calendar_create", "calendar_update", "calendar_delete",
    "email_reply", "email_archive",
    "content_queue", "social_publish",
})
_IRREVERSIBLE = frozenset({"merge_pr", "deploy"})


def risk_for(action_type: str) -> str:
    """Return the canonical server-side risk for an action type. Unknown types
    default to irreversible_outward (fail safe: never auto-run unknown ops)."""
    if action_type in _REVERSIBLE:
        return "reversible_internal"
    if action_type in _PERSONAL_OUTWARD:
        return "personal_outward"
    # merge_pr/deploy and any unknown type are irreversible.
    return "irreversible_outward"


# The set of action types a converse/manager job is allowed to emit. Anything
# not in this set is silently dropped in the worker before finalize. Only safe,
# reversible, repo-scopeable PR ops are here: merge_pr (merge to base) and deploy
# (arbitrary command) are deliberately NOT manager-triggerable, they go through
# the gated pipeline / explicit owner flow, not a free-form chat message.
_CONVERSE_ALLOWLIST = frozenset({
    "close_pr", "comment_pr", "reopen_pr",
})
_CONVERSE_PERSONAL_ALLOWLIST = frozenset({
    "remember",
    "calendar_list", "calendar_get", "calendar_create", "calendar_update",
    "calendar_delete", "email_list", "email_search", "email_read", "email_reply",
    "email_archive", "email_draft", "content_queue",
})
_CONVERSE_TEAM_EMAIL_ALLOWLIST = frozenset({
    "email_list", "email_search", "email_read",
})
_CONVERSE_FIREBASE_ALLOWLIST = frozenset({"set_user_balance"})
_LOW_DISK_NOTIFY_DEDUPE_SECONDS = 300

# PR ops whose target repo + number must be set server-side (never trust the
# model's repo/number: it could otherwise target any repo the gh token reaches).
_PR_NUMBER_OPS = frozenset({"close_pr", "comment_pr", "reopen_pr"})


def _team_autonomy(cfg, team_id: str) -> Autonomy:
    team = cfg.team(team_id)
    return team.autonomy or cfg.company.defaults.autonomy


def _mode_for(autonomy: Autonomy, action_type: str, risk: str) -> str:
    """Resolve the narrow action policy before the risk-wide fallback."""
    return autonomy.actions.get(action_type, getattr(autonomy, risk))


def process_proposed(conn: psycopg.Connection, cfg, *, runner: Optional[Callable] = None) -> int:
    _release_held(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, request_id, team_id, type, risk, destination_ref, "
            "idempotency_key, status, payload "
            "FROM actions WHERE status IN ('proposed','approved') FOR UPDATE SKIP LOCKED")
        rows = cur.fetchall()
    handled = 0
    for action_id, request_id, team_id, atype, risk, destination_ref, idem, status, payload in rows:
        if status == "approved":
            # Already consumed by an approver; skip the policy gate and execute.
            _execute_guarded(conn, str(action_id), cfg=cfg, runner=runner)
        else:
            risk = _effective_risk(cfg, atype, risk, destination_ref)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE actions SET risk=%s, updated_at=now() "
                    "WHERE id=%s AND risk<>%s",
                    (risk, action_id, risk),
                )
            autonomy = _team_autonomy(cfg, team_id)
            mode = _mode_for(autonomy, atype, risk)
            if mode == "auto":
                if _hold_for_quiet_hours(conn, str(action_id), cfg, team_id=team_id,
                                         action_type=atype,
                                         destination_ref=destination_ref,
                                         payload=payload or {}):
                    handled += 1
                    continue
                _execute_guarded(conn, str(action_id), cfg=cfg, runner=runner)
            else:
                _require_approval(conn, str(action_id), request_id)
        handled += 1
    return handled


def _release_held(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE actions
            SET status='proposed',
                payload=payload - 'quiet_hold_until' - 'quiet_hold_reason',
                updated_at=now()
            WHERE status='held'
              AND payload ? 'quiet_hold_until'
              AND (payload->>'quiet_hold_until')::timestamptz <= now()
            """
        )


def _hold_for_quiet_hours(conn: psycopg.Connection, action_id: str, cfg, *,
                          team_id: str, action_type: str,
                          destination_ref: Optional[str], payload: dict) -> bool:
    if action_type != "notify":
        return False
    if _destination_channel_role(cfg, destination_ref) != "control":
        return False
    settings = _notification_settings(cfg, team_id)
    if getattr(settings, "quiet_hours_delivery", "hold") != "hold":
        return False
    if _urgent_notify(settings, payload):
        return False
    until = _quiet_hold_until(settings)
    if until is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status='held', payload=payload||%s::jsonb, "
            "updated_at=now() WHERE id=%s AND status='proposed'",
            (Json({
                "quiet_hold_until": until.astimezone(timezone.utc).isoformat(),
                "quiet_hold_reason": "quiet_hours",
            }), action_id),
        )
        return cur.rowcount == 1


def _notification_settings(cfg, team_id: str):
    team = cfg.team(team_id)
    return team.notifications or cfg.company.defaults.notifications


def _urgent_notify(settings, payload: dict) -> bool:
    if payload.get("urgent") is True:
        return True
    severity = str(payload.get("severity") or payload.get("level") or "").lower()
    urgent_severities = {str(s).lower() for s in getattr(settings, "urgent_severities", [])}
    if severity and severity in urgent_severities:
        return True
    text = str(payload.get("text") or "").lower()
    return any(str(keyword).lower() in text
               for keyword in getattr(settings, "urgent_keywords", []))


def _quiet_hold_until(settings, *, now: datetime | None = None) -> datetime | None:
    quiet = getattr(settings, "quiet_hours", "")
    if quiet is False or str(quiet).strip().lower() in {"", "false", "off", "none"}:
        return None
    if quiet is True:
        quiet = "22:30-08:30"
    try:
        tz = ZoneInfo(getattr(settings, "timezone", "UTC") or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    try:
        start_s, end_s = str(quiet).split("-", 1)
        start_h, start_m = [int(part) for part in start_s.split(":", 1)]
        end_h, end_m = [int(part) for part in end_s.split(":", 1)]
    except (TypeError, ValueError):
        return None
    start = local_now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = local_now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start <= end:
        return end if start <= local_now < end else None
    if local_now >= start:
        return end + timedelta(days=1)
    if local_now < end:
        return end
    return None


def _execute_guarded(conn: psycopg.Connection, action_id: str, *, cfg=None,
                     runner: Optional[Callable] = None) -> None:
    """Run _execute under a per-action SAVEPOINT so one action's send failure
    can't crash the drain loop or leave the shared psycopg conn in an aborted
    state (which would poison every later action this tick). A transient send
    error (httpx timeout/transport, e.g. Evolution slow or restarting) is rolled
    back so the action stays proposed/approved and retries on the next `up`
    tick: the reply is queued, never silently dropped. Any other error marks the
    action failed with the error recorded (no unbounded retry on a real fault)."""
    sp = "act_" + action_id.replace("-", "")
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {sp}")
    try:
        _execute(conn, action_id, cfg=cfg, runner=runner)
        with conn.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {sp}")
    except Exception as exc:
        # ROLLBACK TO SAVEPOINT clears the conn's aborted state and reverts this
        # action's 'executing' write back to its prior proposed/approved status.
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            cur.execute(f"RELEASE SAVEPOINT {sp}")
        if _is_transient(exc):
            log.warning("action %s send failed (transient), will retry: %s", action_id, exc)
        else:
            log.warning("action %s failed: %s", action_id, exc)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE actions SET status='failed', "
                    "payload=payload||%s::jsonb, updated_at=now() WHERE id=%s",
                    (Json({"error": str(exc)}), action_id))


def _is_transient(exc: BaseException) -> bool:
    """Network blips worth retrying (timeout / transport). Lazy httpx import: the
    channel layer owns httpx, the executor must not hard-depend on it."""
    try:
        import httpx
    except Exception:
        return False
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _effective_risk(cfg, action_type: str, declared_risk: str,
                    destination_ref: Optional[str]) -> str:
    """Apply server-side risk rules before the autonomy gate."""
    if action_type in ("reply", "notify", "status"):
        role = _destination_channel_role(cfg, destination_ref)
        if role == "outward":
            return "irreversible_outward"
        if declared_risk == "irreversible_outward":
            return declared_risk
    return risk_for(action_type)


def _destination_channel_role(cfg, destination_ref: Optional[str]) -> Optional[str]:
    if cfg is None or not destination_ref or ":" not in destination_ref:
        return None
    ctype, chat_id = destination_ref.split(":", 1)
    try:
        from argus.v2.channels.base import REGISTRY
        from argus.v2.channels.router import team_for
        if ctype not in REGISTRY:
            return None
        route = team_for(cfg, ctype, chat_id)
        if not route:
            return None
        _team, binding = route
        return getattr(binding, "role", None)
    except Exception:
        return None


def _execute(conn: psycopg.Connection, action_id: str, *, cfg=None,
             runner: Optional[Callable] = None) -> None:
    """Idempotent execution. Real action types (open_pr/ready_pr/merge_pr/deploy) are
    dispatched to handlers via an injectable runner; reply/notify attempt a
    channel send via send.deliver when cfg + destination_ref resolve to a known
    channel binding; otherwise fall back to the slice-1 local: no-op."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status='executing', updated_at=now() "
            "WHERE id=%s AND status IN ('proposed','approved')", (action_id,))
        if cur.rowcount != 1:
            return
        cur.execute(
            "SELECT type, payload, provider_ref, destination_ref, team_id, request_id "
            "FROM actions WHERE id=%s",
            (action_id,))
        atype, payload, existing, destination_ref, team_id, request_id = cur.fetchone()
        if atype == "status":
            # A status action is one self-updating progress line. Unlike every
            # other action, an existing provider_ref does NOT mean "done" - it
            # means "edit that message" (the line advanced to a new stage).
            _execute_status(cur, action_id, cfg, payload or {}, existing, destination_ref)
            return
        if existing:
            # Idempotent: already has a provider_ref from a previous execution.
            cur.execute("UPDATE actions SET status='done', updated_at=now() WHERE id=%s", (action_id,))
            return
        if atype == "remember":
            from argus.v2.knowledge import store as kstore
            p = payload or {}
            kstore.add(conn, cfg, scope=p.get("scope", "team"),
                       team_id=team_id, title=p.get("title", "note"),
                       content=p.get("content", ""), source="agent")
            provider_ref = f"knowledge:{action_id}"
        elif atype in _REAL:
            try:
                kwargs = {"runner": runner} if runner is not None else {}
                provider_ref = _handlers.run(
                    atype, payload or {}, cfg=cfg, team_id=team_id, **kwargs)
            except Exception as e:
                cur.execute(
                    "UPDATE actions SET status='failed', payload=payload||%s::jsonb, "
                    "updated_at=now() WHERE id=%s",
                    (Json({"error": str(e)}), action_id))
                if atype == "set_user_balance":
                    _enqueue_account_result_notify(
                        cur,
                        action_id=action_id,
                        team_id=team_id,
                        destination_ref=destination_ref,
                        error=str(e),
                    )
                return
        elif atype in ("reply", "notify") and cfg is not None and destination_ref:
            suppressed_ref = _suppress_duplicate_low_disk_notify(
                cur,
                action_id=action_id,
                action_type=atype,
                destination_ref=destination_ref,
                payload=payload or {},
            )
            if suppressed_ref:
                cur.execute(
                    "UPDATE actions SET status='done', provider_ref=%s, "
                    "payload=payload||%s::jsonb, updated_at=now() WHERE id=%s",
                    (
                        suppressed_ref,
                        Json({"suppressed_reason": "duplicate_low_disk_notify"}),
                        action_id,
                    ),
                )
                return
            from argus.v2.channels import send as _send
            text = (payload or {}).get("text", "")
            thread_ts = (payload or {}).get("thread_ts")
            # Kwarg only when threaded so plain sends (and test fakes with the
            # 3-arg signature) are untouched.
            kwargs = {"thread_ts": thread_ts} if thread_ts else {}
            channel_ref = _send.deliver(cfg, destination_ref, text, **kwargs)
            provider_ref = channel_ref if channel_ref is not None else f"local:{action_id}"
        else:
            provider_ref = f"local:{action_id}"
        cur.execute(
            "UPDATE actions SET status='done', provider_ref=%s, updated_at=now() "
            "WHERE id=%s", (provider_ref, action_id))
        if atype == "open_pr":
            _enqueue_open_pr_notify(
                cur, action_id=action_id, request_id=request_id, team_id=team_id,
                destination_ref=destination_ref, payload=payload or {},
                provider_ref=provider_ref)
        elif atype in ("email_list", "email_search", "email_read"):
            _enqueue_email_notify(
                cur, action_id=action_id, team_id=team_id,
                destination_ref=destination_ref, action_type=atype,
                payload=payload or {}, provider_ref=provider_ref)
        elif atype == "set_user_balance":
            _enqueue_account_result_notify(
                cur,
                action_id=action_id,
                team_id=team_id,
                destination_ref=destination_ref,
                provider_ref=provider_ref,
            )
            context_id = str((payload or {}).get("support_context_id") or "")
            if context_id:
                cur.execute(
                    "UPDATE conversation_contexts SET status='resolved', updated_at=now() "
                    "WHERE id=%s AND context_type='support_case'",
                    (context_id,),
                )


def _suppress_duplicate_low_disk_notify(
    cur,
    *,
    action_id: str,
    action_type: str,
    destination_ref: str,
    payload: dict,
) -> str | None:
    if action_type != "notify" or not str(destination_ref or "").startswith("whatsapp:"):
        return None
    fingerprint = _low_disk_fingerprint(payload)
    if not fingerprint:
        return None
    cur.execute(
        """
        SELECT id::text
        FROM actions
        WHERE id<>%s
          AND type='notify'
          AND destination_ref=%s
          AND status='done'
          AND provider_ref IS NOT NULL
          AND provider_ref NOT LIKE 'suppressed:%%'
          AND payload->'system_health_fingerprints' ? %s
          AND updated_at > now() - make_interval(secs => %s)
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (
            action_id,
            destination_ref,
            fingerprint,
            _LOW_DISK_NOTIFY_DEDUPE_SECONDS,
        ),
    )
    row = cur.fetchone()
    if not row:
        return None
    prior_action_id = str(row[0])
    cur.execute(
        """
        INSERT INTO alerts (severity, project, fingerprint, message, channel, payload)
        VALUES ('info','general',%s,%s,'log',%s)
        """,
        (
            f"disk:low:send-suppressed:{action_id}",
            f"suppressed duplicate low disk WhatsApp send: {fingerprint}",
            Json({
                "suppressed_fingerprint": fingerprint,
                "suppressed_action_id": action_id,
                "prior_action_id": prior_action_id,
                "dedupe_seconds": _LOW_DISK_NOTIFY_DEDUPE_SECONDS,
            }),
        ),
    )
    return f"suppressed:duplicate_low_disk:{prior_action_id}"


def _low_disk_fingerprint(payload: dict) -> str | None:
    fingerprints = payload.get("system_health_fingerprints")
    if not isinstance(fingerprints, list):
        return None
    for fingerprint in fingerprints:
        value = str(fingerprint)
        if value.startswith("disk:low:"):
            return value
    return None


def _execute_status(cur, action_id: str, cfg, payload: dict, existing,
                    destination_ref) -> None:
    """Send-or-edit the single live status line. First run (no provider_ref)
    posts the message like a normal reply - reliable: a transient send error
    propagates so the executor savepoint retries it, and the receipt is never
    silently lost. Later runs edit that message in place; edits are best-effort
    (status is cosmetic) so a failed edit never poisons the drain - it settles
    done, and a later stage's text change re-arms another attempt. A status
    action only exists for an edit-capable conversation channel (reconcile gates
    that), so the deliver() fallback here is a recovery path (message deleted),
    not per-stage spam."""
    from argus.v2.channels import send as _send
    text = str(payload.get("text", ""))
    resolvable = bool(cfg and destination_ref)
    if not existing:
        ref = _send.deliver(cfg, destination_ref, text) if resolvable else None
        provider_ref = ref if ref is not None else f"local:{action_id}"
        cur.execute("UPDATE actions SET status='done', provider_ref=%s, updated_at=now() "
                    "WHERE id=%s", (provider_ref, action_id))
        _maybe_set_typing(cfg, destination_ref, payload, text)
        return
    try:
        ref = _send.edit(cfg, destination_ref, existing, text) if resolvable else None
        if ref is None and resolvable:
            ref = _send.deliver(cfg, destination_ref, text)  # editable but edit failed: re-post
    except Exception as exc:  # best-effort: a status edit must never break the drain
        log.warning("status edit %s failed (best-effort): %s", action_id, exc)
        ref = None
    cur.execute("UPDATE actions SET status='done', provider_ref=%s, updated_at=now() "
                "WHERE id=%s", (ref or existing, action_id))
    _maybe_set_typing(cfg, destination_ref, payload, text)


def _maybe_set_typing(cfg, destination_ref, payload: dict, text: str) -> None:
    """Fire the native Slack typing-dots for this stage, best-effort, alongside the
    durable status line. Slack-only in practice: other channels expose no
    set_typing() so send.set_typing is a no-op, and a missing thread_ts skips it.
    Never raises - a typing hint must not affect the status write or the drain."""
    thread_ts = (payload or {}).get("thread_ts")
    if not cfg or not destination_ref or not thread_ts:
        return
    from argus.v2.orchestrator import status as _status
    spec = _status.typing_for(text)
    if spec is None:
        return
    status_text, loading = spec
    try:
        from argus.v2.channels import send as _send
        _send.set_typing(cfg, destination_ref, thread_ts, status_text, loading)
    except Exception as exc:
        log.debug("native typing best-effort failed: %s", exc)


# Approval nonce: 128 bits of entropy. This token authorizes an
# irreversible_outward action (PR merge, deploy, outward message), so it must
# not be brute-forceable over the 24h window the way the old 32-bit token was.
_NONCE_BYTES = 16
_NONCE_TRIES = 5


def _insert_approval(cur, action_id: str, request_id, expires) -> str:
    """Insert a pending approval with a fresh unique nonce. Retries on the
    (astronomically unlikely) 128-bit collision instead of silently doing
    nothing, which used to leave the parked action with no nonce - stuck
    forever. Raises loudly if it somehow cannot allocate one."""
    for _ in range(_NONCE_TRIES):
        nonce = secrets.token_hex(_NONCE_BYTES)
        cur.execute(
            "INSERT INTO approvals (action_id, request_id, nonce, expires_at) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (nonce) DO NOTHING RETURNING id",
            (action_id, request_id, nonce, expires))
        if cur.fetchone() is not None:
            return nonce
    raise RuntimeError(f"could not allocate a unique approval nonce for action {action_id}")


def _require_approval(conn: psycopg.Connection, action_id: str, request_id) -> None:
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    with conn.cursor() as cur:
        # Park the action so the next sweep won't re-pick it (no duplicate
        # approvals). consume(approve) re-arms it to 'approved', which
        # process_proposed then executes directly (skipping the policy gate).
        cur.execute(
            "UPDATE actions SET status='awaiting_approval', updated_at=now() "
            "WHERE id=%s AND status='proposed'", (action_id,))
        if cur.rowcount != 1:
            return  # already parked by a concurrent sweep
        _insert_approval(cur, action_id, request_id, expires)
        if request_id is not None:
            cur.execute(
                "UPDATE requests SET status='awaiting_approval', updated_at=now() "
                "WHERE id=%s AND status='open'", (request_id,))


def _enqueue_open_pr_notify(cur, *, action_id: str, request_id, team_id: str,
                            destination_ref: Optional[str], payload: dict,
                            provider_ref: str) -> None:
    if not destination_ref:
        return
    text = _open_pr_notice_text(payload, provider_ref)
    cur.execute(
        "INSERT INTO actions (request_id, team_id, type, risk, "
        "destination_ref, idempotency_key, payload) "
        "VALUES (%s,%s,'notify','reversible_internal',%s,%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (request_id, team_id, destination_ref,
         f"open_pr_notify:{action_id}", Json({"text": text})),
    )


def _open_pr_notice_text(payload: dict, provider_ref: str) -> str:
    request = _bug_report_label(payload.get("request") or payload.get("title") or "")
    summary = str(payload.get("summary_short") or "").strip()
    checks = str(payload.get("checks") or "").strip()
    risk = str(payload.get("risk_summary") or payload.get("risk") or "").strip()
    status = _status_line(risk)
    title = "Argus PR ready" if status == "checked and verified" else "Argus PR needs review"
    lines = [
        title,
        f"Request: {_quote(_short(request or 'Argus PR', 220))}",
        f"Fix: {_quote(_short(summary or 'See PR for fix details.', 220))}",
    ]
    if provider_ref:
        lines.append(f"PR: {provider_ref}")
    lines.append(f"Status: {status}")
    check_lines = _check_lines(checks)
    if check_lines:
        lines.append("Checks:")
        lines.extend(f"- {line}" for line in check_lines)
    risk_lines = _risk_lines(risk)
    if risk_lines:
        lines.append("Review notes:")
        lines.extend(f"- {line}" for line in risk_lines)
    return "\n".join(lines)


def _bug_report_label(value: str) -> str:
    text = " ".join(str(value or "").split())
    lowered = text.lower()
    if lowered.startswith("argus: "):
        text = text[7:].strip()
        lowered = text.lower()
    if lowered.startswith("investigate bug report "):
        _head, sep, tail = text.partition(":")
        if sep and tail.strip():
            text = tail.strip()
    elif lowered.startswith("investigate why "):
        text = text[len("Investigate "):].strip()
    for marker in (", identify owning", ". Identify owning", ", root cause", ". Identify root cause"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def _quote(value: str) -> str:
    return '"' + value.replace('"', "'") + '"'


def _check_lines(checks: str) -> list[str]:
    return [part.strip() for part in str(checks or "").split(";") if part.strip()]


def _risk_lines(risk: str) -> list[str]:
    text = _clean_risk_text(risk)
    if not text:
        return []
    lowered = text.lower()
    if lowered.startswith("low"):
        return []
    parts = [part.strip() for part in text.split(";") if part.strip()]
    return [_short(part, 260) for part in parts]


def _clean_risk_text(risk: str) -> str:
    text = " ".join(str(risk or "").split())
    return re.sub(r"Command \[.*\] timed out after ([0-9.]+) seconds",
                  r"browser command timed out after \1 seconds", text)


def _verification_line(checks: str, risk: str) -> str:
    checks = _short(checks, 220)
    lowered = f"{checks} {risk}".lower()
    if not checks:
        return ""
    if "fail" in lowered or "no decision" in lowered or "needs review" in lowered:
        return checks
    return f"checked and verified ({checks})"


def _status_line(risk: str) -> str:
    lowered = risk.lower()
    if not lowered or lowered.startswith("low"):
        return "checked and verified"
    if "browser_verify" in lowered or "browser verify" in lowered:
        return "needs review, browser verification failed"
    if "senior failed" in lowered:
        return "needs review, senior did not approve"
    if "qa failed" in lowered:
        return "needs review, QA failed"
    return f"needs review, {_short(risk, 160)}"


def _enqueue_email_notify(cur, *, action_id: str, team_id: str,
                          destination_ref: Optional[str], action_type: str,
                          payload: dict, provider_ref: str) -> None:
    if not destination_ref:
        return
    text = _email_result_text(action_type, payload, provider_ref)
    if not text:
        return
    cur.execute(
        "INSERT INTO actions (team_id, type, risk, destination_ref, "
        "idempotency_key, payload) "
        "VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (team_id, destination_ref, f"email_result:{action_id}",
         Json({"text": text})),
    )


def _enqueue_account_result_notify(cur, *, action_id: str, team_id: str,
                                   destination_ref: Optional[str],
                                   provider_ref: str = "", error: str = "") -> None:
    if not destination_ref:
        return
    if error:
        text = f"Credit balance update failed: {_short(error, 300)}"
    else:
        try:
            result = json.loads(provider_ref or "{}")
        except json.JSONDecodeError:
            result = {}
        before = result.get("before")
        after = result.get("after")
        text = f"Credits updated: {before} -> {after}."
    cur.execute(
        "INSERT INTO actions (team_id, type, risk, destination_ref, "
        "idempotency_key, payload) "
        "VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (team_id, destination_ref, f"account_result:{action_id}",
         Json({"text": text, "urgent": True})),
    )


def _email_result_text(action_type: str, payload: dict, provider_ref: str) -> str:
    if action_type in ("email_list", "email_search"):
        label = "Email search" if action_type == "email_search" else "Email list"
        try:
            rows = json.loads(provider_ref or "[]")
        except json.JSONDecodeError:
            rows = []
        if not isinstance(rows, list) or not rows:
            return f"{label}: no results."
        lines = [f"{label}: {len(rows)} result(s)."]
        for item in rows[:5]:
            if not isinstance(item, dict):
                continue
            sender = _short(item.get("sender") or item.get("from") or "unknown", 80)
            subject = _short(item.get("subject") or "(no subject)", 100)
            thread_id = _short(item.get("thread_id") or item.get("threadId") or "", 80)
            snippet = _short(item.get("snippet") or "", 160)
            line = f"- {sender} | {subject}"
            if thread_id:
                line += f" | thread {thread_id}"
            if snippet:
                line += f" | {snippet}"
            lines.append(line)
        return "\n".join(lines)
    if action_type == "email_read":
        thread_id = _short(payload.get("thread_id") or payload.get("threadId") or "", 80)
        body = _short(provider_ref or "", 1800)
        title = f"Email read: thread {thread_id}" if thread_id else "Email read:"
        return f"{title}\n{body}".strip()
    return ""


def _short(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
