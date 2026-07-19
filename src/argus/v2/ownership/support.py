from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import unquote
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from argus.v2.db import pool
from argus.v2.ownership import store
from argus.v2.support.apps_script import AppsScriptTransport


SUPPORT_REPLY_RISK = "personal_outward"
_SUPPORT_SOURCE_TYPES = frozenset({"support_apps_script", "apps_script_support"})
_BLOCKED_VARIANTS = {
    "billing": (
        "billing", "billings", "billed", "billing issue", "billing request",
        "subscription", "subscriptions",
    ),
    "refund": ("refund", "refunds", "refunded", "refunding"),
    "payment": (
        "payment", "payments", "payment issue", "payment failed",
        "payment failure", "credit card", "credit cards", "debit card",
        "debit cards",
    ),
    "charge": ("charge", "charges", "charged", "charging"),
    "charge_dispute": (
        "chargeback", "chargebacks", "charge dispute", "charge disputes",
        "disputed charge", "disputed charges",
    ),
    "invoice": ("invoice", "invoices", "invoiced", "invoicing"),
    "account_access": (
        "access", "account access", "access account", "accessing account",
        "account locked", "locked account", "locked out", "unlock account",
        "unlocked account", "account recovery", "recover account",
    ),
    "security": (
        "security", "security breach", "security breaches", "breach",
        "breaches", "breached", "insecure", "insecurity",
    ),
    "privacy": (
        "privacy", "privacy request", "privacy requests", "gdpr",
        "personal data",
    ),
    "legal": (
        "legal", "legally", "lawyer", "lawyers", "attorney", "attorneys",
        "lawsuit", "lawsuits", "litigation",
    ),
    "deletion": (
        "delete", "deletes", "deleted", "deleting", "deletion", "deletions",
        "remove account", "removing account", "close account", "closing account",
    ),
    "password": (
        "password", "passwords", "pass word", "pass words", "passcode",
        "passcodes",
    ),
    "login": (
        "login", "logins", "log in", "logs in", "logged in", "logging in",
        "sign in", "signin", "signed in", "signing in", "authentication",
        "authenticate", "authenticated",
    ),
    "account_ownership": (
        "account owner", "account owners", "account ownership",
        "ownership account", "ownership of account", "owns account",
        "owned account",
    ),
}
_BLOCKED_CATEGORIES = frozenset(_BLOCKED_VARIANTS)
_CONFUSABLES = str.maketrans({
    "а": "a", "с": "c", "е": "e", "і": "i", "ј": "j", "о": "o",
    "р": "p", "ѕ": "s", "х": "x", "у": "y", "ɑ": "a", "ο": "o",
    "ρ": "p", "χ": "x", "ν": "v", "ι": "i",
})


@dataclass(frozen=True)
class SupportPolicyDecision:
    allowed: bool
    reason: str
    sensitive_term: str | None = None


def _percent_decode_bounded(value: str, *, rounds: int = 3) -> str:
    current = value
    for _ in range(rounds):
        decoded = unquote(current, errors="replace")
        if decoded == current:
            break
        current = decoded
    return current


def _normalized(value: str) -> tuple[str, str]:
    text = _percent_decode_bounded(str(value or ""))
    text = unicodedata.normalize("NFKC", text).casefold().translate(_CONFUSABLES)
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
        and unicodedata.category(char) not in {"Cf", "Cc", "Cs"}
    )
    spaced = re.sub(r"[^a-z0-9]+", " ", text)
    spaced = " ".join(spaced.split())
    return spaced, re.sub(r"[^a-z0-9]+", "", text)


def _canonical_category(value: str) -> str:
    spaced, _compact = _normalized(value)
    return spaced.replace(" ", "_")


def _variant_concept(value: str) -> str | None:
    if len(str(value or "")) > 50_000:
        return "oversized_content"
    spaced, _compact = _normalized(value)
    tokens = spaced.split()
    for concept, variants in _BLOCKED_VARIANTS.items():
        for variant in variants:
            variant_spaced, variant_compact = _normalized(variant)
            variant_tokens = variant_spaced.split()
            if any(
                tokens[start:start + len(variant_tokens)] == variant_tokens
                for start in range(0, len(tokens) - len(variant_tokens) + 1)
            ):
                return concept
            if any(
                "".join(tokens[start:start + width]) == variant_compact
                for width in range(
                    1, min(12, len(variant_compact), len(tokens)) + 1)
                for start in range(0, len(tokens) - width + 1)
            ):
                return concept
    return None


def _sensitive_term(*values: str) -> str | None:
    for value in values:
        concept = _variant_concept(value)
        if concept:
            return concept
    return None


def classify_for_auto_send(
    team,
    decision,
    raw_thread: str,
    *,
    sender: str = "",
    subject: str = "",
) -> SupportPolicyDecision:
    policy = team.ownership.support
    if not team.ownership.enabled:
        return SupportPolicyDecision(False, "team ownership is disabled")
    if not policy.enabled:
        return SupportPolicyDecision(False, "support ownership is disabled")
    if not policy.auto_send_low_risk:
        return SupportPolicyDecision(False, "low-risk support auto-send is disabled")
    reply = str(getattr(decision, "reply", "") or "").strip()
    if not reply:
        return SupportPolicyDecision(False, "reply is empty")
    if bool(getattr(decision, "needs_guidance", False)):
        return SupportPolicyDecision(False, "reply needs guidance")
    if bool(getattr(decision, "should_escalate", False)):
        return SupportPolicyDecision(False, "reply requires escalation")
    category = _canonical_category(str(getattr(decision, "category", "") or ""))
    configured = {
        _canonical_category(value) for value in policy.blocked_categories
    }
    category_concept = _variant_concept(category.replace("_", " "))
    if category in (_BLOCKED_CATEGORIES | configured) or category_concept:
        return SupportPolicyDecision(False, f"blocked support category: {category}")
    try:
        confidence = float(getattr(decision, "confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < policy.min_confidence:
        return SupportPolicyDecision(False, "support confidence is below policy")
    if str(getattr(decision, "risk", "") or "").strip().lower() != "low":
        return SupportPolicyDecision(False, "support risk is not low")
    term = _sensitive_term(sender, subject, raw_thread, reply)
    if term:
        return SupportPolicyDecision(
            False, f"sensitive support term detected: {term}", term
        )
    return SupportPolicyDecision(True, "low-risk known answer")


def open_or_update_obligation(
    conn: psycopg.Connection,
    *,
    team,
    source,
    thread_id: str,
    sender: str,
    subject: str,
    raw_thread: str,
    decision,
):
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise ValueError("support thread id is required")
    if source.type not in _SUPPORT_SOURCE_TYPES or source.team != team.name:
        raise ValueError("support source is not configured for this team")
    reply = str(getattr(decision, "reply", "") or "").strip()
    reply_sha = sha256(reply.encode("utf-8")).hexdigest()
    evidence = {
        "source_name": source.name,
        "thread_id": thread_id,
        "sender": str(sender or ""),
        "subject": str(subject or ""),
        "raw_thread": str(raw_thread or ""),
        "reply": reply,
        "reply_sha256": reply_sha,
        "category": str(getattr(decision, "category", "") or ""),
        "risk": str(getattr(decision, "risk", "") or ""),
        "confidence": getattr(decision, "confidence", 0.0),
        "needs_guidance": bool(getattr(decision, "needs_guidance", False)),
        "should_escalate": bool(getattr(decision, "should_escalate", False)),
    }
    obligation = store.upsert(
        conn,
        team_id=team.name,
        kind="support",
        fingerprint=f"support:{source.name}:{thread_id}",
        title=f"Support: {subject or thread_id}",
        source_ref=source.name,
        definition_of_done={"provider_reply": True, "thread_id": thread_id},
    )
    if obligation.kind != "support" or obligation.source_ref != source.name:
        raise RuntimeError("support obligation identity collision")
    if obligation.status == "open":
        obligation = store.transition(
            conn,
            obligation.id,
            to_status="working",
            reason="support thread received",
            evidence=evidence,
        )
    elif obligation.status not in {"done", "failed"}:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE team_obligations SET evidence=evidence || %s, "
                "updated_at=clock_timestamp() WHERE id=%s",
                (Jsonb(evidence), obligation.id),
            )
        obligation = store.get(conn, obligation.id)
    return obligation


def await_approval(
    conn: psycopg.Connection,
    obligation,
    *,
    reason: str,
    policy: SupportPolicyDecision | None = None,
):
    evidence = {"policy_reason": reason}
    if policy and policy.sensitive_term:
        evidence["sensitive_term"] = policy.sensitive_term
    if obligation.status == "awaiting_approval":
        return store.transition(
            conn, obligation.id, to_status="awaiting_approval",
            reason=reason, evidence=evidence,
        )
    return store.transition(
        conn, obligation.id, to_status="awaiting_approval",
        reason=reason, evidence=evidence,
    )


def _block(conn: psycopg.Connection, obligation_id, reason: str):
    obligation = store.get(conn, obligation_id)
    if obligation is None:
        raise ValueError(f"obligation not found: {obligation_id}")
    if obligation.status in {"done", "failed"}:
        return obligation
    if obligation.status == "blocked":
        return store.transition(
            conn, obligation.id, to_status="blocked", reason=reason,
            evidence={"support_error": reason},
        )
    return store.transition(
        conn, obligation.id, to_status="blocked", reason=reason,
        evidence={"support_error": reason},
    )


def queue_reply_action(
    conn: psycopg.Connection,
    *,
    team,
    source,
    obligation,
    decision,
) -> tuple[UUID, bool]:
    policy = classify_for_auto_send(
        team,
        decision,
        str(obligation.evidence.get("raw_thread") or ""),
        sender=str(obligation.evidence.get("sender") or ""),
        subject=str(obligation.evidence.get("subject") or ""),
    )
    if not policy.allowed:
        await_approval(
            conn, obligation, reason=policy.reason, policy=policy
        )
        raise RuntimeError(f"support policy denied: {policy.reason}")
    thread_id = str(obligation.evidence.get("thread_id") or "")
    reply_sha = str(obligation.evidence.get("reply_sha256") or "")
    reply_hash16 = reply_sha[:16]
    key = f"support_reply:{team.name}:{thread_id}:{reply_hash16}"
    payload = {
        "obligation_id": str(obligation.id),
        "source_name": source.name,
        "thread_id": thread_id,
        "reply_hash": reply_sha,
    }
    destination = f"support:{source.name}"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO actions
              (team_id, type, risk, destination_ref, idempotency_key, payload)
            VALUES (%s, 'support_reply', %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                team.name, SUPPORT_REPLY_RISK, destination, key, Jsonb(payload),
            ),
        )
        row = cur.fetchone()
        inserted = row is not None
        if row is None:
            cur.execute(
                "SELECT id, team_id, type, risk, destination_ref, payload "
                "FROM actions WHERE idempotency_key=%s",
                (key,),
            )
            existing = cur.fetchone()
            if existing is None or (
                existing["team_id"] != team.name
                or existing["type"] != "support_reply"
                or existing["risk"] != SUPPORT_REPLY_RISK
                or existing["destination_ref"] != destination
                or dict(existing["payload"] or {}) != payload
            ):
                _block(conn, obligation.id, f"idempotency collision for {key}")
                raise RuntimeError(f"idempotency collision for {key}")
            row = existing
    action_id = row["id"]
    store.link_action(conn, obligation.id, action_id)
    return action_id, inserted


def _load_persisted(conn: psycopg.Connection, payload: dict, team_id: str):
    obligation_id = str(payload.get("obligation_id") or "")
    try:
        UUID(obligation_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("support action obligation_id is invalid") from exc
    obligation = store.get(conn, obligation_id)
    if obligation is None or obligation.team_id != team_id or obligation.kind != "support":
        raise RuntimeError("support action obligation does not match team")
    evidence = obligation.evidence
    canonical = {
        "obligation_id": str(obligation.id),
        "source_name": str(evidence.get("source_name") or ""),
        "thread_id": str(evidence.get("thread_id") or ""),
        "reply_hash": str(evidence.get("reply_sha256") or ""),
    }
    if any(payload.get(key) != value for key, value in canonical.items()):
        raise RuntimeError("support action payload does not match persisted state")
    return obligation, evidence


def _configured_source(cfg, team_id: str, source_name: str):
    candidates = [*cfg.company.sources, *cfg.team(team_id).sources]
    matches = [
        source for source in candidates
        if source.name == source_name
        and source.type in _SUPPORT_SOURCE_TYPES
        and source.team == team_id
    ]
    if len(matches) != 1:
        raise RuntimeError("configured support source is missing or ambiguous")
    source = matches[0]
    scfg = source.config or {}
    if not scfg.get("url") or not source.secret:
        raise RuntimeError("configured support transport is incomplete")
    return source


def _reserve_delivery_attempt(obligation_id: UUID) -> bool:
    with pool.connect() as marker_conn:
        with marker_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE team_obligations
                SET evidence=evidence || %s, updated_at=clock_timestamp()
                WHERE id=%s
                  AND NOT evidence ? 'delivery_attempted_at'
                  AND status NOT IN ('done', 'failed')
                """,
                (
                    Jsonb({
                        "delivery_attempted_at": datetime.now(timezone.utc).isoformat(),
                    }),
                    obligation_id,
                ),
            )
            reserved = cur.rowcount == 1
        marker_conn.commit()
    return reserved


def _persist_success(conn: psycopg.Connection, obligation_id: UUID, provider_ref: str):
    obligation = store.get(conn, obligation_id)
    if obligation is None:
        raise ValueError(f"obligation not found: {obligation_id}")
    if obligation.status == "done":
        if obligation.provider_ref != provider_ref:
            raise RuntimeError("support provider reference collision")
        return obligation
    if obligation.status == "blocked":
        obligation = store.transition(
            conn, obligation.id, to_status="working",
            reason="support delivery confirmed", evidence={"provider_ref": provider_ref},
        )
    if obligation.status == "awaiting_approval":
        obligation = store.transition(
            conn, obligation.id, to_status="working",
            reason="support reply explicitly authorized",
            evidence={"provider_ref": provider_ref},
        )
    obligation = store.transition(
        conn, obligation.id, to_status="verifying",
        reason="support provider accepted reply",
        evidence={"provider_ref": provider_ref},
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET provider_ref=%s WHERE id=%s",
            (provider_ref, obligation.id),
        )
    return store.transition(
        conn, obligation.id, to_status="done",
        reason="support reply delivery recorded",
        evidence={"provider_ref": provider_ref},
    )


def run_reply_action(
    conn: psycopg.Connection,
    cfg,
    team_id: str,
    payload: dict,
) -> str:
    obligation = None
    try:
        obligation, evidence = _load_persisted(conn, payload, team_id)
        team = cfg.team(team_id)
        source = _configured_source(
            cfg, team_id, str(evidence.get("source_name") or "")
        )
        decision = SimpleNamespace(
            reply=str(evidence.get("reply") or ""),
            category=str(evidence.get("category") or ""),
            risk=str(evidence.get("risk") or ""),
            confidence=evidence.get("confidence", 0.0),
            needs_guidance=bool(evidence.get("needs_guidance")),
            should_escalate=bool(evidence.get("should_escalate")),
        )
        policy = classify_for_auto_send(
            team,
            decision,
            str(evidence.get("raw_thread") or ""),
            sender=str(evidence.get("sender") or ""),
            subject=str(evidence.get("subject") or ""),
        )
        if not policy.allowed:
            raise RuntimeError(f"support policy denied: {policy.reason}")
        if not _reserve_delivery_attempt(obligation.id):
            raise RuntimeError(
                "support delivery outcome is uncertain; owner reconciliation required"
            )
        transport = AppsScriptTransport(
            url=source.config["url"],
            key=source.secret,
            timeout=float(source.config.get("timeout", 30)),
        )
        thread_id = str(evidence["thread_id"])
        reply = str(evidence["reply"])
        transport.reply(thread_id, reply)
        transport.mark_read(thread_id)
        transport.archive(thread_id)
        provider_ref = f"support:{thread_id}"
        _persist_success(conn, obligation.id, provider_ref)
        return provider_ref
    except Exception as exc:
        if obligation is not None:
            _block(conn, obligation.id, str(exc))
        raise


def reconcile_explicit_reply(
    conn: psycopg.Connection,
    *,
    team_id: str,
    source_name: str,
    thread_id: str,
    provider_ref: str,
) -> bool:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM team_obligations "
            "WHERE team_id=%s AND fingerprint=%s",
            (team_id, f"support:{source_name}:{thread_id}"),
        )
        row = cur.fetchone()
    if row is None:
        return False
    _persist_success(conn, row["id"], provider_ref)
    return True
