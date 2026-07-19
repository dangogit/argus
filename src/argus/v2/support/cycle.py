"""Pure Python support cycle runner for propose-mode customer support."""
from __future__ import annotations

import html
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.types.json import Json

from argus.engine import EngineOutageError, run_agent
from argus.v2.config import loader
from argus.v2.engine_runner import run_with_fallback
from argus.v2.ownership import support as ownership_support
from argus.v2.rules import context as rules_context
from argus.v2.skills import registry as skills
from argus.v2.support.apps_script import (
    AppsScriptTransport,
    AppsScriptTransportError,
    EmailSummary,
)
from argus.v2.support import state


@dataclass(frozen=True)
class SupportResult:
    proposed: int = 0
    sent: int = 0
    archived: int = 0
    escalated: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class DraftDecision:
    reply: str
    should_escalate: bool = False
    category: str = "support_request"
    reason: str = ""
    risk: str = "unknown"
    confidence: float = 0.0
    needs_guidance: bool = False
    guidance_question: str = ""


@dataclass(frozen=True)
class ContextResponse:
    handled: bool
    reply: str = ""
    context_status: str | None = None


def run_project(conn: psycopg.Connection, cfg, team_id: str) -> SupportResult:
    team = cfg.team(team_id)
    source = _support_source(cfg, team_id)
    scfg = source.config or {}
    limit = int(scfg.get("daily_limit", scfg.get("limit", 10)))
    if not scfg.get("url"):
        raise ValueError(f"support source {source.name} is missing config.url")
    if not source.secret:
        raise ValueError(f"support source {source.name} is missing secret")
    transport = AppsScriptTransport(
        url=scfg["url"],
        key=source.secret,
        timeout=float(scfg.get("timeout", 30)),
    )
    try:
        emails = transport.list_unread(limit)
    except AppsScriptTransportError as exc:
        print(
            f"[argus] support: {team_id} transport unavailable: {exc}",
            file=sys.stderr,
        )
        return SupportResult(skipped=limit)
    result = SupportResult()
    for email in emails:
        result = _handle_email(conn, cfg, team, source, scfg, transport, email, result)
    conn.commit()
    return result


def handle_guidance_reply(conn: psycopg.Connection, cfg, team_id: str, text: str) -> bool:
    parsed = _parse_guidance_reply(text)
    if not parsed:
        return False
    guidance_id, answer = parsed
    req = state.guidance_request(team_id, guidance_id)
    if not req:
        return False
    source = _support_source(cfg, team_id)
    scfg = source.config or {}
    rejected = _rejects_send(answer)
    explicit_reply = _reply_from_answer(answer)
    proposed_reply = (req.get("proposed_reply") or "").strip()
    if not rejected and not explicit_reply and proposed_reply and _approves_send(answer):
        explicit_reply = proposed_reply
    status = "rejected" if rejected else "sent" if explicit_reply else "answered"
    state.resolve_guidance(team_id, guidance_id, answer, status=status)
    _remember_guidance(conn, cfg, team_id, req, answer)
    if rejected:
        _notify(conn, team_id, scfg, f"Support guidance {guidance_id} learned. No customer reply sent.")
        return True
    if not explicit_reply:
        _notify(conn, team_id, scfg,
                f"Support guidance {guidance_id} learned. No customer reply sent. "
                "Use send: <exact reply> to email the customer.")
        return True
    body = explicit_reply
    _send_customer_reply(source, scfg, req, body)
    state.record(team_id, req["thread_id"], "replied",
                 req.get("from", ""), req.get("subject", ""))
    _notify(conn, team_id, scfg, f"Support guidance {guidance_id} learned and customer reply sent.")
    return True


def handle_context_message(conn: psycopg.Connection, cfg, *, team_id: str,
                           context: dict, text: str) -> ContextResponse:
    req = _request_from_context(team_id, context)
    if not req:
        return ContextResponse(False)
    answer = (text or "").strip()
    if not answer:
        return ContextResponse(False)
    if _is_support_question(answer):
        return ContextResponse(True, reply=_context_answer(req))
    explicit_reply = _reply_from_answer(answer)
    proposed = (req.get("proposed_reply") or "").strip()
    if not explicit_reply and proposed and _approves_send(answer):
        explicit_reply = proposed
    if not explicit_reply and _approves_send(answer) and not proposed:
        return ContextResponse(
            True,
            reply="No proposed reply to send. Use send: <exact reply> to email the customer.",
        )
    if not explicit_reply and _requests_email_recheck(answer):
        return ContextResponse(True, reply=_context_retry_answer(req))
    rejected = _rejects_send(answer)
    if not explicit_reply and not rejected and _requests_email_lookup(answer):
        return ContextResponse(False)

    source = _support_source(cfg, team_id)
    scfg = source.config or {}
    if rejected:
        state.resolve_guidance(team_id, req["id"], answer, status="rejected")
        _remember_guidance(conn, cfg, team_id, req, answer)
        return ContextResponse(
            True,
            reply="Learned. No customer reply sent.",
            context_status="learned",
        )
    if explicit_reply:
        _send_customer_reply(source, scfg, req, explicit_reply)
        state.resolve_guidance(team_id, req["id"], answer, status="sent")
        _remember_guidance(conn, cfg, team_id, req, answer)
        state.record(team_id, req["thread_id"], "replied",
                     req.get("from", ""), req.get("subject", ""))
        return ContextResponse(
            True,
            reply="Sent and learned.",
            context_status="resolved",
        )
    return ContextResponse(False)


def learn_context_message(conn: psycopg.Connection, cfg, *, team_id: str,
                          context_ref: str, guidance: str) -> bool:
    """Persist manager-classified support guidance after semantic routing."""
    answer = (guidance or "").strip()
    req = state.guidance_request(team_id, context_ref)
    if not req or not answer:
        return False
    state.resolve_guidance(team_id, context_ref, answer, status="answered")
    _remember_guidance(conn, cfg, team_id, req, answer)
    return True


def _handle_email(conn, cfg, team, source, scfg, transport: AppsScriptTransport,
    email: EmailSummary, result: SupportResult) -> SupportResult:
    project = team.name
    if state.latest_action(project, email.thread_id) in {
        "needs_human", "escalated", "archived", "replied", "draft_ready",
        "guidance_requested", "guidance_answered", "guidance_rejected",
        "auto_replied",
    }:
        return _bump(result, "skipped")
    if state.has_ready_draft(project, email.thread_id):
        return _bump(result, "skipped")

    verdict = classify_email(email, own_domain=_domain(scfg.get("email", "")),
                             automated_domains=scfg.get("automated_domains", []),
                             escalation_keywords=scfg.get("escalation_keywords", []),
                             non_support_keywords=scfg.get("non_support_keywords", []))
    if verdict == "automated":
        transport.archive(email.thread_id)
        transport.mark_read(email.thread_id)
        state.record(project, email.thread_id, "archived", email.sender, email.subject)
        return _bump(result, "archived")
    if verdict == "escalate":
        thread = transport.read(email.thread_id) or email.snippet or email.subject
        obligation = _support_obligation(
            conn, team, source, email, thread,
            DraftDecision(
                reply="", should_escalate=True, category="escalation",
                reason="deterministic high-risk triage", risk="high",
            ),
        )
        if obligation is not None:
            ownership_support.await_approval(
                conn, obligation, reason="deterministic high-risk triage"
            )
        transport.mark_read(email.thread_id)
        question = "This looks high-risk. What should we do?"
        guidance = state.register_guidance_request(
            project, email.thread_id, email.sender, email.subject,
            question, "", thread)
        _register_support_context(conn, project, scfg, guidance.id, email, thread,
                                  question, "")
        dispatched = _dispatch_investigation(conn, cfg, project, scfg, email, thread)
        _notify(conn, team.name, scfg,
                _guidance_text(project, guidance.id, email, thread, question,
                               investigation_dispatched=dispatched))
        return _bump(result, "escalated")

    thread = transport.read(email.thread_id)
    if not thread:
        state.record(project, email.thread_id, "failed", email.sender, email.subject, "empty thread")
        return _bump(result, "skipped")

    failure: dict[str, str] = {}
    decision = draft_decision(conn, cfg, team.name, thread=thread, sender=email.sender,
                              subject=email.subject, tone=scfg.get("tone", "friendly"),
                              repo=getattr(team.project, "repo", None) if team.project else None,
                              failure_out=failure)
    if decision is None:
        reason = failure.get("reason", "unknown")
        obligation = _support_obligation(
            conn, team, source, email, thread,
            DraftDecision(
                reply="", should_escalate=True, category="unknown",
                reason=reason, risk="unknown", needs_guidance=True,
            ),
        )
        if obligation is not None:
            ownership_support.await_approval(
                conn, obligation, reason=f"support draft failed: {reason}"
            )
        state.record(project, email.thread_id, "failed", email.sender, email.subject, reason)
        transport.mark_read(email.thread_id)
        question = (
            f"Draft failed ({reason}). Worker read the thread but did not return "
            "valid support JSON. What should we tell this customer?"
        )
        guidance = state.register_guidance_request(
            project, email.thread_id, email.sender, email.subject,
            question,
            "", thread)
        _register_support_context(
            conn, project, scfg, guidance.id, email, thread,
            question,
            "")
        _notify(conn, team.name, scfg,
                _guidance_text(project, guidance.id, email, thread, question))
        return _bump(result, "escalated")
    if decision.category == "non_support":
        transport.archive(email.thread_id)
        transport.mark_read(email.thread_id)
        state.record(project, email.thread_id, "archived", email.sender, email.subject,
                     decision.reason or "llm_non_support")
        return _bump(result, "archived")
    if decision.should_escalate or decision.needs_guidance:
        obligation = _support_obligation(
            conn, team, source, email, thread, decision
        )
        if obligation is not None:
            policy = ownership_support.classify_for_auto_send(
                team, decision, thread, sender=email.sender,
                subject=email.subject,
            )
            ownership_support.await_approval(
                conn, obligation, reason=policy.reason, policy=policy
            )
        transport.mark_read(email.thread_id)
        question = decision.guidance_question or decision.reason or "What should we tell this customer?"
        guidance = state.register_guidance_request(
            project, email.thread_id, email.sender, email.subject,
            question, decision.reply, thread)
        _register_support_context(conn, project, scfg, guidance.id, email, thread,
                                  question, decision.reply)
        _notify(conn, team.name, scfg,
                _guidance_text(project, guidance.id, email, thread, question, decision.reply))
        return _bump(result, "escalated")

    obligation = _support_obligation(conn, team, source, email, thread, decision)
    if obligation is not None:
        policy = ownership_support.classify_for_auto_send(
            team, decision, thread, sender=email.sender, subject=email.subject
        )
        if policy.allowed:
            ownership_support.queue_reply_action(
                conn,
                team=team,
                source=source,
                obligation=obligation,
                decision=decision,
            )
            return _bump(result, "proposed")
        ownership_support.await_approval(
            conn, obligation, reason=policy.reason, policy=policy
        )

    if _notify_level(scfg) == "guidance_only":
        # Every owner touchpoint is one concise guidance format; a would-be
        # draft becomes an OK-to-send guidance request so "send" works
        # in-thread via the conversation context.
        transport.mark_read(email.thread_id)
        question = "Proposed reply ready. OK to send?"
        guidance = state.register_guidance_request(
            project, email.thread_id, email.sender, email.subject,
            question, decision.reply, thread)
        _register_support_context(conn, project, scfg, guidance.id, email, thread,
                                  question, decision.reply)
        _notify(conn, team.name, scfg,
                _guidance_text(project, guidance.id, email, thread, question,
                               decision.reply))
        return _bump(result, "proposed")

    draft = state.register_draft(
        project, email.thread_id, email.sender, email.subject, decision.reply,
        transport=scfg.get("transport", source.type),
    )
    _notify(conn, team.name, scfg,
            f"Support draft {draft.id} ready ({project}).\nReply through Argus support approval to send.\n\n{decision.reply}")
    return _bump(result, "proposed")


def classify_email(email: EmailSummary, *, own_domain: str = "",
                   automated_domains=None, escalation_keywords=None,
                   non_support_keywords=None) -> str:
    escalation_keywords = [k.lower() for k in _as_list(escalation_keywords)]
    sender_l = email.sender.lower()
    body_l = f"{email.subject} {email.snippet}".lower()
    if any(word in body_l for word in ("refund", "chargeback", "legal", "lawyer", "gdpr", "delete my account")):
        return "escalate"
    if any(k and k in body_l for k in escalation_keywords):
        return "escalate"
    if "noreply" in sender_l or "no-reply" in sender_l:
        return "automated"
    domain = _domain(email.sender)
    if domain and domain == own_domain:
        return "automated"
    if any(word in body_l for word in ("help", "stuck", "can't", "cannot", "charged", "billing", "account")):
        return "support"
    return "support"


def draft_reply(cfg, team_id: str, *, thread: str, sender: str, subject: str,
                tone: str, repo: str | None) -> str | None:
    decision = draft_decision(None, cfg, team_id, thread=thread, sender=sender,
                              subject=subject, tone=tone, repo=repo)
    return decision.reply if decision and not decision.should_escalate else None


def draft_decision(conn, cfg, team_id: str, *, thread: str, sender: str, subject: str,
                   tone: str, repo: str | None,
                   failure_out: dict[str, str] | None = None) -> DraftDecision | None:
    engine = loader.resolve_engine(cfg, team_id, "support").engine
    role = cfg.team(team_id).role("support")
    text = f"{subject}\n{thread}"
    guidance = _guidance_context(conn, cfg, team_id, text)
    rules = rules_context.block_for(conn, cfg, team_id=team_id)
    skill_block = skills.block_for("support", text, allow=(role.skills or None))
    prompt = _support_prompt(role.prompt, tone, sender, subject, thread,
                             guidance, rules, skill_block)
    prev_cwd = os.environ.get("ARGUS_AGENT_CWD")
    prev_project = os.environ.get("ARGUS_PROJECT")
    if repo:
        os.environ["ARGUS_AGENT_CWD"] = repo
    os.environ["ARGUS_PROJECT"] = team_id
    try:
        # Support runs with no fallback engine: an outage becomes a guidance
        # request to the owner instead of a retry on another engine.
        output = run_with_fallback(run_agent, engine, None, prompt)
    except EngineOutageError:
        _set_failure(failure_out, "engine_outage")
        return None
    finally:
        _restore_env("ARGUS_AGENT_CWD", prev_cwd)
        _restore_env("ARGUS_PROJECT", prev_project)
    parsed = _parse_json(output)
    if not parsed or parsed.get("should_escalate") is True:
        if not parsed:
            _set_failure(failure_out, "invalid_json")
            return None
        return DraftDecision(
            reply=_reply_to_text(str(parsed.get("reply") or "")),
            should_escalate=True,
            category=_category(parsed),
            reason=str(parsed.get("reason") or ""),
            risk=str(parsed.get("risk") or "high").lower(),
            confidence=_float(parsed.get("confidence")),
            needs_guidance=bool(parsed.get("needs_guidance")),
            guidance_question=str(parsed.get("guidance_question") or ""),
        )
    reply = parsed.get("reply")
    category = _category(parsed)
    if category == "non_support":
        return DraftDecision(
            reply="",
            should_escalate=False,
            category=category,
            reason=str(parsed.get("reason") or ""),
            risk=str(parsed.get("risk") or "low").lower(),
            confidence=_float(parsed.get("confidence")),
            needs_guidance=bool(parsed.get("needs_guidance")),
            guidance_question=str(parsed.get("guidance_question") or ""),
        )
    if not isinstance(reply, str) or not reply.strip():
        _set_failure(failure_out, "empty_reply")
        return None
    return DraftDecision(
        reply=_reply_to_text(reply),
        should_escalate=False,
        category=category,
        reason=str(parsed.get("reason") or ""),
        risk=str(parsed.get("risk") or "unknown").lower(),
        confidence=_float(parsed.get("confidence")),
        needs_guidance=bool(parsed.get("needs_guidance")),
        guidance_question=str(parsed.get("guidance_question") or ""),
    )


def _support_prompt(role_prompt: str, tone: str, sender: str, subject: str,
                    thread: str, guidance: str = "", rules: str = "",
                    skill_block: str = "") -> str:
    guidance_block = f"\n\nReusable guidance:\n{guidance}" if guidance else ""
    base = (
        "You are a customer support agent. Read the full email thread first. "
        "First classify the email as support_request or non_support. "
        "non_support includes newsletters, receipts, invoices, vendor notices, "
        "privacy-policy updates, terms updates, marketing, and alerts not asking us for help. "
        "Return only JSON with keys category, reply, should_escalate, reason, risk, confidence, "
        "needs_guidance, and guidance_question. If category is non_support, reply must be empty. "
        "Escalate for refunds, legal, account deletion, or anything you cannot answer from context.\n\n"
        f"Tone: {tone}\nFrom: {sender}\nSubject: {subject}\nThread:\n{thread}{guidance_block}"
    )
    parts = [role_prompt.strip(), rules.strip(), skill_block.strip(), base]
    return "\n\n".join(part for part in parts if part)


def _notify_level(scfg: dict) -> str:
    level = str(scfg.get("notify_level", "all")).strip().lower()
    return level if level in {"all", "guidance_only"} else "all"


def _notify(conn: psycopg.Connection, team_id: str, scfg: dict, text: str) -> None:
    dest = scfg.get("notify_destination", "cli:local")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    idem = f"support-notify:{team_id}:{digest}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, destination_ref, idempotency_key, payload) "
            "VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (team_id, dest, idem, Json({"text": text})),
        )


def _support_source(cfg, team_id: str):
    for source in [*cfg.company.sources, *cfg.team(team_id).sources]:
        if source.team == team_id and source.type in {"support_apps_script", "apps_script_support"}:
            return source
    raise KeyError(f"no support_apps_script source for team {team_id}")


def _support_obligation(conn, team, source, email: EmailSummary, thread: str,
                        decision: DraftDecision):
    ownership = getattr(team, "ownership", None)
    if (
        conn is None
        or not getattr(ownership, "enabled", False)
        or not getattr(getattr(ownership, "support", None), "enabled", False)
    ):
        return None
    return ownership_support.open_or_update_obligation(
        conn,
        team=team,
        source=source,
        thread_id=email.thread_id,
        sender=email.sender,
        subject=email.subject,
        raw_thread=thread,
        decision=decision,
    )


def _bump(result: SupportResult, field: str) -> SupportResult:
    values = result.__dict__.copy()
    values[field] += 1
    return SupportResult(**values)


def _parse_guidance_reply(text: str) -> tuple[str, str] | None:
    match = re.search(r"\bsupport\s+([0-9]{8}T[0-9]{6}Z-[0-9]{5})\s+(.+)",
                      text or "", flags=re.I | re.S)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _guidance_text(project: str, guidance_id: str, email: EmailSummary,
                   thread: str, question: str, proposed_reply: str = "",
                   investigation_dispatched: bool = False) -> str:
    lines = [
        f"Support needs you ({project})",
        f'From {email.sender}: "{email.subject}"',
        "",
        "Customer says:",
        _customer_message(thread),
        "",
        question,
    ]
    if investigation_dispatched:
        lines += ["", "An investigation was auto-dispatched to the team "
                      "pipeline; findings arrive separately."]
    if proposed_reply:
        lines += ["", "Agent suggests:", proposed_reply, "",
                  'Reply "send" to send it, send: <your text> for a different '
                  "reply. Anything else teaches Argus."]
    else:
        lines += ["", "Reply send: <exact customer reply> to email the "
                      "customer. Anything else teaches Argus."]
    lines += ["", f"(ID {guidance_id})"]
    return "\n".join(lines)


def _register_support_context(conn: psycopg.Connection, project: str, scfg: dict,
                              guidance_id: str, email: EmailSummary,
                              thread: str, question: str,
                              proposed_reply: str) -> None:
    if conn is None:
        return
    from argus.v2.orchestrator import context_router
    channel_ref = scfg.get("notify_destination", "cli:local")
    payload = {
        "id": guidance_id,
        "project": project,
        "thread_id": email.thread_id,
        "from": email.sender,
        "subject": email.subject,
        "question": question,
        "proposed_reply": proposed_reply,
        "thread": thread,
    }
    context_router.register_context(
        conn,
        team_id=project,
        channel_ref=channel_ref,
        context_type="support_case",
        context_ref=guidance_id,
        summary=_support_case_summary(payload),
        payload=payload,
        ttl_hours=int(scfg.get("guidance_context_ttl_hours", 72)),
    )


_INVESTIGATION_THREAD_LIMIT = 4000


def _dispatch_investigation(conn: psycopg.Connection | None, cfg, project: str,
                            scfg: dict, email: EmailSummary, thread: str) -> bool:
    """Auto-dispatch a team-pipeline investigation for a high-risk escalated
    email, opt-in via scfg escalate_dispatch (default off, unchanged behavior).
    Reuses the same signal-ingest + open_request path as PM auto-fix dispatch
    (see argus.v2.pm.autofix.dispatch) instead of inventing a parallel enqueue.
    Idempotent per thread_id: ingest_signal dedupes on (source, dedup_key) and
    open_request dedupes on (team_id, fingerprint) including terminal requests,
    so a rerun of the same thread never opens a second investigation."""
    if conn is None or not scfg.get("escalate_dispatch"):
        return False
    from argus.v2.ingress import events
    from argus.v2.orchestrator import pipeline

    truncated = thread.strip()
    if len(truncated) > _INVESTIGATION_THREAD_LIMIT:
        truncated = truncated[:_INVESTIGATION_THREAD_LIMIT] + "..."
    text = (
        "Investigate this customer complaint: check logs, database state, and "
        "recent deploys relevant to the report; produce findings and, if a code "
        "fix is clear, a fix.\n\n"
        f"Subject: {email.subject}\n"
        f"From: {email.sender}\n\n"
        f"Thread:\n{truncated}"
    )
    fingerprint = f"support-escalate:{project}:{email.thread_id}"
    event_id = events.ingest_signal(
        conn, cfg, team=project, source="support-escalate",
        fingerprint=fingerprint, payload={"text": text, "thread_id": email.thread_id},
    )
    request_id = pipeline.open_request(
        conn, cfg, event_id=event_id, team_id=project,
        conversation_id=None, fingerprint=fingerprint, dedup_terminal=True,
    )
    return request_id is not None


def _request_from_context(team_id: str, context: dict) -> dict | None:
    guidance_id = str(context.get("context_ref") or "")
    req = state.guidance_request(team_id, guidance_id)
    if req:
        return req
    payload = context.get("payload") or {}
    if isinstance(payload, dict) and payload.get("id") and payload.get("thread_id"):
        return payload
    return None


def _context_answer(req: dict) -> str:
    lines = [
        f"Support case {req.get('id', '')}",
        f"From: {req.get('from', '')}",
        f"Subject: {req.get('subject', '')}",
        "",
        f"Question for you: {req.get('question', 'What should we do?')}",
        "",
        "Customer request:",
        _thread_excerpt(req.get("thread", "")),
        "",
        'No customer reply sent. Reply "send" to send the proposed reply, '
        "guidance to teach Argus, or send: <exact reply> to email the customer.",
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def _context_retry_answer(req: dict) -> str:
    lines = [
        "I checked the email context again.",
        f"From: {req.get('from', '')}",
        f"Subject: {req.get('subject', '')}",
        "",
        f"Question: {req.get('question', 'What should we do?')}",
        "",
        "Customer request:",
        _thread_excerpt(req.get("thread", "")),
        "",
        "No email sent. To send a customer reply, use send: <exact reply>.",
        "To ignore it, say: no reply needed.",
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def _support_case_summary(req: dict) -> str:
    return " | ".join(part for part in [
        str(req.get("from") or ""),
        str(req.get("subject") or ""),
        _thread_excerpt(str(req.get("thread") or ""), limit=180),
    ] if part)


def _thread_excerpt(thread: str, *, limit: int = 900) -> str:
    text = (thread or "").replace("\r", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


_QUOTE_MARKERS = re.compile(
    r"^\s*(>|On .{0,120} wrote:|From: |Sent from my )", re.I)


def _customer_message(thread: str, *, limit: int = 500) -> str:
    """The customer's own words: cut at the first quoted-history or signature
    marker, collapse whitespace, truncate. Falls back to the generic excerpt
    when stripping leaves nothing (fully-quoted thread)."""
    lines: list[str] = []
    for line in (thread or "").replace("\r", "\n").splitlines():
        if not lines and re.match(r"^\s*From: ", line, re.I):
            continue
        if _QUOTE_MARKERS.match(line) or line.strip() == "--":
            break
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    text = re.sub(r"[ \t]+", " ", text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text or _thread_excerpt(thread, limit=limit)


def _send_customer_reply(source, scfg: dict, req: dict, body: str) -> str:
    transport = AppsScriptTransport(
        url=scfg["url"],
        key=source.secret,
        timeout=float(scfg.get("timeout", 30)),
    )
    transport.reply(req["thread_id"], body)
    transport.mark_read(req["thread_id"])
    transport.archive(req["thread_id"])
    provider_ref = f"support:{req['thread_id']}"
    project = str(req.get("project") or "")
    if project:
        state.reconcile_explicit_reply(
            project, source.name, req["thread_id"], provider_ref
        )
    return provider_ref


def _is_support_question(text: str) -> bool:
    body = (text or "").strip().lower()
    if not body:
        return False
    if body.endswith("?"):
        return True
    return any(phrase in body for phrase in (
        "what did", "what does", "what is", "who is", "who sent",
        "summarize", "summary", "details", "context", "what request",
        "what she request", "what he request", "what are they asking",
        "what is she asking", "what is he asking", "what happened",
    ))


def _requests_email_recheck(text: str) -> bool:
    body = " ".join((text or "").strip().lower().split())
    if not body:
        return False
    if body in {
        "try again",
        "retry",
        "check again",
        "check it again",
        "look again",
        "read again",
        "check email",
        "check the email",
        "read email",
        "read the email",
        "show email",
        "show the email",
    }:
        return True
    return any(phrase in body for phrase in (
        "check the email again",
        "read the email again",
        "show me the email",
        "what email",
    ))


def _requests_email_lookup(text: str) -> bool:
    body = " ".join((text or "").strip().lower().split())
    if not body:
        return False
    if body.startswith((
        "find email",
        "find the email",
        "search email",
        "search the email",
        "look for email",
        "look for the email",
        "look up email",
        "look up the email",
    )):
        return True
    padded = f" {body} "
    return " email from " in padded or " mail from " in padded


def _guidance_context(conn, cfg, team_id: str, query: str) -> str:
    if conn is None:
        return ""
    try:
        from argus.v2.knowledge import store as kstore
        rows = kstore.search(conn, cfg, team_id=team_id, query=query, k=3,
                             sources=["support-guidance", "support-rule"])
        if not rows:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, content
                    FROM knowledge
                    WHERE (scope='company' OR team_id=%s)
                      AND source = ANY(%s)
                    ORDER BY created_at DESC, id DESC
                    LIMIT 3
                    """,
                    (team_id, ["support-guidance", "support-rule"]),
                )
                rows = [{"title": title, "content": content}
                        for title, content in cur.fetchall()]
    except Exception:
        return ""
    return "\n\n".join(f"{r['title']}: {r['content']}" for r in rows)


def _remember_guidance(conn, cfg, team_id: str, req: dict, answer: str) -> None:
    from argus.v2.knowledge import store as kstore
    rejected = _rejects_send(answer)
    content = "\n".join([
        f"Customer subject: {req.get('subject', '')}",
        f"Customer sender: {req.get('from', '')}",
        f"Owner guidance: {answer}",
        (
            "Rule: ignore similar non-support vendor notices and send no customer reply."
            if rejected else
            "Use for similar support replies. Do not auto-send if refund, legal, deletion, or billing risk appears."
        ),
    ])
    kstore.add(conn, cfg, scope="team", team_id=team_id,
               title=f"Support guidance: {req.get('subject', 'customer reply')}",
               content=content, source="support-rule" if rejected else "support-guidance")


def _rejects_send(answer: str) -> bool:
    body = (answer or "").strip().lower()
    if body.startswith(("no", "don't", "do not", "dont", "stop", "hold", "reject")):
        return True
    return any(phrase in body for phrase in (
        "ignore it",
        "ignore this",
        "not support",
        "not a support email",
        "no reply needed",
        "no customer reply",
        "archive this",
    ))


_APPROVE_PHRASES = {
    "yes", "y", "yep", "yeah", "yup", "ok", "okay", "k",
    "send", "send it", "send that", "send this", "send the draft",
    "send the reply", "send reply", "send as is", "send as-is",
    "approve", "approved", "approve it", "go", "go ahead", "do it",
    "ship it", "ship", "lgtm", "looks good", "sounds good", "perfect",
    "yes send", "yes send it", "ok send", "ok send it", "ok go",
    "\U0001f44d", "\U0001f44d\U0001f3fb", "\U0001f44d\U0001f3fc",
    "\U0001f44d\U0001f3fd", "\U0001f44d\U0001f3fe", "\U0001f44d\U0001f3ff",
}


def _approves_send(answer: str) -> bool:
    body = " ".join((answer or "").strip().lower().split())
    body = body.strip(".!")
    return body in _APPROVE_PHRASES


def _reply_from_answer(answer: str) -> str:
    match = re.search(r"\b(?:send|reply)\s*:\s*(.+)", answer or "", flags=re.I | re.S)
    return _reply_to_text(match.group(1)) if match else ""


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _domain(value: str) -> str:
    match = re.search(r"@([^>\s]+)", value or "")
    if match:
        return match.group(1).lower()
    if "@" in (value or ""):
        return value.rsplit("@", 1)[-1].lower()
    return ""


def _category(parsed: dict) -> str:
    raw = str(parsed.get("category") or "support_request").strip().lower()
    if raw in {"non_support", "newsletter", "vendor_notice", "receipt", "invoice", "policy_update"}:
        return "non_support"
    return "support_request"


def _set_failure(failure_out: dict[str, str] | None, reason: str) -> None:
    if failure_out is not None:
        failure_out["reason"] = reason


def _parse_json(text: str) -> dict | None:
    stripped = (text or "").strip()
    candidates = [stripped]
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, flags=re.S)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    obj = re.search(r"\{.*\}", stripped, flags=re.S)
    if obj:
        candidates.append(obj.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _reply_to_text(body: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", body)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous
