"""Pipeline state machine: open a request, enqueue stages with deterministic
keys, advance on completion with bounded branching, propagate dead->failed.
All advancement is one transaction + ON CONFLICT, so it is safe under double
delivery or two orchestrators."""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from typing import Optional

import psycopg
from psycopg.types.json import Json

from argus.v2 import alerts
from argus.v2.config import loader
from argus.v2.orchestrator import bug_dedup
from argus.v2.orchestrator import status
from argus.v2.pm import memory as pm_memory
from argus.v2.pm import scan as pm_scan
from argus.v2.queue import jobs
from argus.v2.queue.models import Job
from argus.v2.rules import context as rules_context
from argus.v2.roles import contracts
from argus.v2.skills import registry as skills

log = logging.getLogger("argus.pipeline")

# Signal payloads whose only content is one of these is housekeeping noise, not
# work. Without this gate an internal "no findings" alert opened a build request,
# failed QA, and pinged the owner (owner-reported feedback loop, 2026-06-19).
_SIGNAL_NOISE = ("produced no findings", "skipped or empty", "no new findings")

_PIPELINE_CHECKPOINTS = (
    "CHECKPOINTS:\n"
    "- Send a short progress update when you start, after each risky external "
    "side effect, and before the final response.\n"
    "- For merge, deploy, repair, or live ops work, name completed milestones "
    "when applicable: investigated, changed, deployed, repaired-live-state, "
    "verified, summary-ready.\n"
    "- QA-sensitive work cannot close unless the transcript documents the "
    "access path, item-by-item disposition, verification coverage, and the "
    "post-fix follow-up condition.\n"
    "- For REVIEW items marked fixed and deployed, keep an explicit manual QA "
    "follow-up until owner or manual QA confirmation arrives.\n"
    "- Before emitting qa-fail or a failing PR summary, classify each failure as "
    "code regression, environment blocker, expected cancellation, stale status, "
    "or unknown.\n"
    "- If interrupted, the transcript must show completed external side effects "
    "and remaining work."
)

_MANAGER_CHECKPOINTS = (
    "CHECKPOINTS:\n"
    "- If a REVIEW item is already fixed and deployed but awaiting owner/manual "
    "QA confirmation, do not silently ignore it. Reply with a manual QA "
    "follow-up asking the owner to confirm.\n"
    "- QA-sensitive work cannot close unless the transcript documents the "
    "access path, item-by-item disposition, verification coverage, and the "
    "post-fix follow-up condition."
)


def is_actionable(payload: Optional[dict]) -> bool:
    """True if a signal payload is worth opening a request for. Drops empty
    payloads and known internal-noise messages; keeps anything with real text
    (including structured connector payloads that have no ``text`` field)."""
    payload = payload or {}
    blob = " ".join(str(v) for v in payload.values() if v).strip().lower()
    if not blob:
        return False
    return not any(marker in blob for marker in _SIGNAL_NOISE)


def collapse_repeat(text: str) -> str:
    """Collapse a phrase repeated back-to-back to a single copy, e.g.
    "לא עובד לא עובד" -> "לא עובד". WhatsApp dispatch sometimes doubled the
    request text; this keeps PR titles and request bodies clean."""
    words = " ".join((text or "").split()).split(" ")
    n = len(words)
    if n < 2 or words == [""]:
        return " ".join(words)
    for period in range(1, n // 2 + 1):
        if n % period == 0 and all(words[i] == words[i % period] for i in range(n)):
            return " ".join(words[:period])
    return " ".join(words)


def too_vague_to_dispatch(text: str) -> bool:
    """Degenerate-input floor: a dispatch task this thin can't be acted on, so
    ask for detail instead of opening a junk PR. Semantic vagueness (e.g. a
    two-word "not working") is the manager prompt's job; this only catches the
    truly empty / single-token cases so it never rejects a real terse task."""
    cleaned = collapse_repeat(text)
    if not cleaned:
        return True
    words = cleaned.split(" ")
    return len(words) < 2 or len(cleaned) < 6


def _stage_key(request_id: str, stage_index: int, branch_iter: int) -> str:
    raw = f"{request_id}:stage:{stage_index}:iter:{branch_iter}"
    return hashlib.sha256(raw.encode()).hexdigest()


def open_request(conn: psycopg.Connection, cfg, *, event_id: str, team_id: str,
                 conversation_id: Optional[str], fingerprint: Optional[str] = None,
                 dedup_terminal: bool = False) -> Optional[str]:
    with conn.cursor() as cur:
        if fingerprint and dedup_terminal:
            # Signal-origin work: a connector row maps to one fingerprint for its
            # whole lifetime, so a re-emitted row must NOT open a second pipeline
            # even after the first request has gone done/failed. The partial
            # unique index only guards active requests, so check terminal ones
            # explicitly. A genuinely new occurrence arrives as a new row -> new
            # fingerprint -> new request, so this never suppresses real work.
            cur.execute(
                "SELECT 1 FROM requests WHERE team_id=%s AND fingerprint=%s LIMIT 1",
                (team_id, fingerprint),
            )
            if cur.fetchone():
                return None
        if _pm_equivalence_enabled(fingerprint):
            cur.execute("SELECT payload FROM events WHERE id=%s", (event_id,))
            row = cur.fetchone()
            payload = dict(row[0] or {}) if row else {}
            if equivalent_pm_request(conn, team_id, payload, fingerprint):
                return None
        # Off by default (checked first: cheaper than the payload lookup below,
        # and keeps the common case a no-op with zero extra queries).
        dedup_on = bug_dedup.dedup_enabled(cfg, team_id)
        bug_ref = _bug_ref_for_event(conn, event_id) if dedup_on else None
        if bug_ref:
            # Company-wide duplicate-work guard: this choke point is shared by
            # every dispatch path (converse dispatch, triage dispatch, research
            # recommend-fix, PM autofix), so checking here covers signal-driven
            # and chat-driven dispatches alike. Real incident (2026-07-01): the
            # same bug id opened tadam PR #324 and tadam-agents PR #302.
            dup = bug_dedup.find_duplicate(conn, bug_ref)
            if dup:
                dup_request_id, dup_team_id = dup
                _notify_duplicate_skipped(conn, cfg, team_id=team_id,
                                          conversation_id=conversation_id,
                                          bug_ref=bug_ref,
                                          dup_request_id=dup_request_id,
                                          dup_team_id=dup_team_id)
                return None
        cur.execute(
            """
            INSERT INTO requests (event_id, team_id, conversation_id, fingerprint, bug_ref)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (team_id, fingerprint)
              WHERE fingerprint IS NOT NULL
                AND status IN ('open','awaiting_approval')
            DO NOTHING
            RETURNING id
            """,
            (event_id, team_id, conversation_id, fingerprint, bug_ref),
        )
        row = cur.fetchone()
        if not row:
            return None  # active request already exists for this fingerprint
        request_id = str(row[0])
    enqueue_stage(conn, cfg, request_id=request_id, stage_index=0)
    return request_id


def equivalent_pm_request(conn: psycopg.Connection, team_id: str, payload: dict,
                          fingerprint: Optional[str]) -> Optional[str]:
    if not _pm_equivalence_enabled(fingerprint):
        return None
    target = _pm_work_signature(payload)
    if not target:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id::text, r.fingerprint, e.payload
            FROM requests r
            JOIN events e ON e.id = r.event_id
            WHERE r.team_id=%s
              AND r.fingerprint IS NOT NULL
              AND r.fingerprint <> %s
              AND (
                r.fingerprint LIKE 'retro-change:%%'
                OR r.fingerprint LIKE 'converse:%%'
              )
            ORDER BY r.created_at DESC
            LIMIT 100
            """,
            (team_id, fingerprint or ""),
        )
        rows = cur.fetchall()
    for request_id, _existing_fp, existing_payload in rows:
        if _pm_signatures_match(target, _pm_work_signature(dict(existing_payload or {}))):
            return request_id
    return None


def _pm_equivalence_enabled(fingerprint: Optional[str]) -> bool:
    return bool(
        fingerprint
        and (fingerprint.startswith("retro-change:")
             or fingerprint.startswith("converse:"))
    )


def _pm_work_signature(payload: dict) -> tuple[str, str, str] | None:
    text = _norm_work_part(
        payload.get("task_text")
        or payload.get("text")
        or payload.get("message")
        or payload.get("title")
    )
    if not text:
        return None
    lineage = _norm_work_part(
        payload.get("lineage")
        or payload.get("request_lineage")
        or payload.get("source_lineage")
    )
    theme = _norm_work_part(payload.get("theme") or payload.get("retro_theme"))
    return text, lineage, theme


def _pm_signatures_match(left: tuple[str, str, str] | None,
                         right: tuple[str, str, str] | None) -> bool:
    if not left or not right or left[0] != right[0]:
        return False
    for idx in (1, 2):
        if left[idx] and right[idx] and left[idx] != right[idx]:
            return False
    return True


def _norm_work_part(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        value = " ".join(sorted(str(item) for item in value if str(item)))
    text = " ".join(str(value).casefold().split())
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in text).split()
    )


def _bug_ref_for_event(conn: psycopg.Connection, event_id) -> Optional[str]:
    if not event_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM events WHERE id=%s", (event_id,))
        row = cur.fetchone()
    if not row:
        return None
    payload = row[0] or {}
    return bug_dedup.extract_bug_ref(payload=payload, text=payload.get("text"))


def _notify_duplicate_skipped(conn: psycopg.Connection, cfg, *, team_id: str,
                              conversation_id: Optional[str], bug_ref: str,
                              dup_request_id: str, dup_team_id: str) -> None:
    """Surface the skip instead of silently dropping the dispatch: log an alert
    for signal-origin work, or reply in the conversation for chat-origin work,
    same channel each path already uses for a 'no fix needed' / failure note."""
    text = (f"skipped duplicate: bug {bug_ref} already being handled by "
            f"team {dup_team_id} (request {dup_request_id})")
    if not conversation_id:
        alerts.record(conn, severity="info", project=str(team_id),
                      fingerprint=f"dup-bug-dispatch:{bug_ref}:{dup_request_id}",
                      message=text, channel="log",
                      payload={"bug_ref": bug_ref, "duplicate_request_id": dup_request_id,
                               "duplicate_team_id": dup_team_id})
        return
    dest = _control_destination(conn, cfg, team_id, conversation_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, destination_ref, "
            "idempotency_key, payload) VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (team_id, dest, f"dup_bug_dispatch:{bug_ref}:{dup_request_id}",
             Json({"text": text})))


def _branch_iter(conn: psycopg.Connection, request_id: str, stage_index: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT branch_counters FROM requests WHERE id=%s", (request_id,))
        counters = cur.fetchone()[0] or {}
    # A rework of an earlier stage invalidates all downstream approvals. Use the
    # highest earlier branch counter so QA and senior get fresh idempotency keys
    # after developer rework.
    return max(int(counters.get(str(idx), 0)) for idx in range(stage_index + 1))


def enqueue_stage(conn: psycopg.Connection, cfg, *, request_id: str, stage_index: int) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, conversation_id, event_id FROM requests WHERE id=%s",
                    (request_id,))
        team_id, conversation_id, event_id = cur.fetchone()
    team = cfg.team(team_id)
    role_name = team.pipeline.stages[stage_index]
    # Advance the live status line for this turn (no-op if none exists).
    status.set_status(conn, str(event_id) if event_id else None, status.stage_line(role_name))
    eng = loader.resolve_engine(cfg, team_id, role_name)
    role = team.role(role_name)
    text = _request_text(conn, event_id)
    snapshot = {"engine": eng.engine, "model": eng.model, "prompt": role.prompt,
                "config_hash": _config_hash(cfg),
                # Same as converse/triage/research: selects the hermes per-project
                # learning profile (HERMES_HOME) so pipeline roles share project
                # memory. No-op for codex/echo. (Was missing -> developer ran with
                # ARGUS_PROJECT unset -> _default profile.)
                "project": team_id}
    _add_rules(conn, cfg, snapshot, team_id)
    _add_skills(snapshot, role_name, role.skills, text)
    _add_pipeline_checkpoints(snapshot)
    _add_prompt_hash(snapshot)
    proj = team.project
    if proj is not None and getattr(proj, "allow_code_mode", False) \
            and role.kind in ("builder", "worker"):
        snapshot["allow_code_mode"] = True
    if proj is not None and getattr(proj, "allow_network", False) \
            and role.kind in ("builder", "worker"):
        # Network-enabled sandbox so the agent can run gh/git fetch+push (inspect
        # PRs, rebase against remote base, push). Writes stay confined to the
        # worktree. Frozen into the snapshot so the job replays deterministically.
        snapshot["network"] = True
    snapshot.update(_role_snapshot_extra(role_name))
    branch_iter = _branch_iter(conn, request_id, stage_index)
    job_id = jobs.enqueue(
        conn, team_id=team_id, kind="pipeline", role=role_name, stage=stage_index,
        idempotency_key=_stage_key(request_id, stage_index, branch_iter),
        exec_snapshot=snapshot, payload={"text": text},
        request_id=request_id, event_id=str(event_id),
        conversation_id=str(conversation_id) if conversation_id else None,
    )
    log.info("request %s advanced to stage %d (%s) team=%s job=%s",
             request_id, stage_index, role_name, team_id, job_id,
             extra={"job_id": job_id, "team_id": team_id, "request_id": request_id})
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET current_stage=%s, updated_at=now() WHERE id=%s",
                    (stage_index, request_id))


def _role_snapshot_extra(role: str) -> dict:
    """Hook for tests to inject scripted output per role. No-op in production."""
    return {}


def on_job_done(conn: psycopg.Connection, cfg, job: Job) -> None:
    """Advance the pipeline with verdict-aware branching. Dead -> request failed.
    Converse jobs are handled before the pipeline guard."""
    if job.kind == "converse":
        _handle_converse(conn, cfg, job)
        return
    if job.kind == "triage":
        _handle_triage(conn, cfg, job)
        return
    if job.kind == "research":
        _handle_research(conn, cfg, job)
        return
    if job.kind != "pipeline" or job.request_id is None:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT status, result FROM jobs WHERE id=%s", (job.id,))
        row = cur.fetchone()
        status, result = row[0], row[1]
        cur.execute("SELECT status FROM requests WHERE id=%s", (job.request_id,))
        req_status = cur.fetchone()[0]
    if req_status not in ("open",):
        return
    if status in ("dead", "failed"):
        _fail(conn, cfg, job.request_id, "Pipeline job failed before completion.")
        return
    if status != "done":
        return

    parsed = (result or {}).get("parsed", {})
    team = cfg.team(job.team_id)
    role = job.role
    # Advisory mode: no project means no test_cmd and no structured output expected.
    # Both qa and senior default to pass/approve so echo pipelines advance linearly.
    advisory = getattr(team, "project", None) is None

    if role == _stage_role(team, 0):
        # developer: if it investigated and no fix is warranted (ready=false in a
        # project team), close cleanly with the analysis - no qa/senior, no PR,
        # no fabricated change. Otherwise advance to qa.
        if not advisory and not contracts.dev_ready(parsed):
            analysis = parsed.get("analysis") or parsed.get("summary") or "No fix warranted."
            if _looks_blocked(parsed, analysis):
                # Blocked (no repo/network/gh access, 404, perms) is NOT "no fix
                # needed". Report the block honestly and record it as such so the
                # owner sees the real failure and pm_lessons isn't poisoned with a
                # false no-change. (Owner hit this: GitHub-blocked dev run was
                # reported as "Investigated, no fix needed".)
                _record_memory_outcome(conn, job.request_id, "blocked", analysis)
                _no_fix_close(conn, cfg, job.request_id, analysis, blocked=True)
            elif _awaits_manual_qa(parsed, analysis):
                _record_memory_outcome(conn, job.request_id, "manual-qa", analysis)
                _manual_qa_close(conn, cfg, job.request_id, analysis)
            elif _recommends_fix(parsed, analysis):
                # Third class: the developer DIAGNOSED a concrete, located fix but
                # produced no diff. Closing this as "no fix needed" would hide a
                # real, unapplied defect. Try once more to convert the diagnosis
                # into an applied fix (bounded by the same rework budget _loop_back
                # uses); only if that budget is exhausted do we surface it as a
                # visible "found, not fixed" outcome - never a clean no-fix close.
                _handle_recommended_fix(conn, cfg, job, team, analysis)
            else:
                _record_memory_outcome(conn, job.request_id, "no-change", analysis)
                _no_fix_close(conn, cfg, job.request_id, analysis)
        else:
            _next(conn, cfg, job.request_id, 1)
    elif _is_qa(team, role):
        verdict = contracts.qa_verdict(parsed, (result or {}).get("test_exit"))
        if verdict == "pass" or (advisory and "verdict" not in parsed and (result or {}).get("test_exit") is None):
            _advance_or_done(conn, cfg, job, team)
        else:
            _loop_back(conn, cfg, job, team, to_role="developer",
                       detail=_parsed_failure_detail(parsed))
    elif _is_browser_verify(team, role):
        # The verdict is written by the worker from the browser-use result, or set
        # to 'pass' when the diff touched no UI files (skipped). Fail-closed: any
        # missing/non-pass verdict routes back to the developer.
        bv_verdict = (parsed or {}).get("verdict")
        if bv_verdict == "pass" or (advisory and "verdict" not in (parsed or {})):
            _advance_or_done(conn, cfg, job, team)
        else:
            _loop_back(conn, cfg, job, team, to_role="developer",
                       detail=_parsed_failure_detail(parsed))
    elif role == "senior":
        decision = contracts.senior_decision(parsed)
        if decision == "approve" or (advisory and "decision" not in parsed):
            _approve_done(conn, cfg, job)
        else:
            _loop_back(conn, cfg, job, team, to_role="developer",
                       detail=_parsed_failure_detail(parsed))
    else:
        # Generic linear advance for any other role
        next_index = job.stage + 1
        if next_index >= len(team.pipeline.stages):
            with conn.cursor() as cur:
                cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s",
                            (job.request_id,))
        else:
            enqueue_stage(conn, cfg, request_id=job.request_id, stage_index=next_index)


def _stage_role(team, stage_index: int) -> Optional[str]:
    """Name of the role at stage_index, or None if out of range."""
    stages = team.pipeline.stages
    if 0 <= stage_index < len(stages):
        return stages[stage_index]
    return None


def _index(team, role_name: str) -> Optional[int]:
    """Stage index of a role name, or None if not in the pipeline."""
    try:
        return team.pipeline.stages.index(role_name)
    except ValueError:
        return None


# Signs the developer could not do the work (no access) rather than judged no
# fix needed. Checked against the explicit status field first, then analysis text.
_BLOCKED_MARKERS = (
    "blocked", "could not", "couldn't", "cannot ", "can't ", "unable",
    "denied", "unauthorized", "not authorized", "no access", "without access",
    "forbidden", "401", "403", "404", "shell access", "network", "connector returns",
)


def _looks_blocked(parsed: dict, analysis: str) -> bool:
    """True when a ready=false dev result means 'I was prevented from working'
    (no repo/network/gh/perms) rather than 'I looked and no fix is warranted'.
    Honors an explicit status='blocked' when the engine provides one."""
    status = str(parsed.get("status", "")).lower()
    if status in ("blocked", "error", "failed"):
        return True
    if status in ("no_fix", "no_fix_needed", "no-change", "ok", "done"):
        return False
    text = (analysis or "").lower()
    return any(m in text for m in _BLOCKED_MARKERS)


# A ready=false dev result that DIAGNOSED a concrete, located fix but produced no
# diff. This is the third class of ready=false (alongside blocked vs genuine
# no-fix): the developer found a real bug and a real fix yet left it unapplied,
# so closing it as "no fix needed" hides a live, diagnosed defect (owner case:
# "A focused code fix is warranted: require amount > 0 ... [file.vue:498]").
# Strong recommend-INTENT phrases: each asserts a change is needed, so they do
# not flip meaning under negation the way bare "root cause"/"should be" do (those
# match "no root cause found" / "should be correct" and were dropped after review).
_RECOMMEND_FIX_MARKERS = (
    "fix is warranted", "focused code fix", "code fix is warranted",
    "should require", "apply the fix", "apply this fix", "must require",
    "the fix is", "recommended fix", "should add", "should change",
    "should guard", "should validate", "needs a fix", "fix should",
    "the correct fix", "patch should",
)
# Genuine no-fix / negation phrases. If any appears, the analysis is saying
# nothing needs changing - never treat it as a diagnosed-but-unapplied fix, even
# if a recommend marker also matched (negated context).
_NO_FIX_MARKERS = (
    "no fix", "no change", "nothing to fix", "nothing to change",
    "no root cause", "is correct", "is fine", "works as", "as expected",
    "expected behavior", "expected behaviour", "no defect", "not a bug",
    "behaves correctly", "no code change",
)
_MANUAL_QA_FIXED_MARKERS = (
    "fixed and deployed", "deployed and fixed", "already fixed",
    "fix is deployed", "deployed fix", "deployed the fix",
)
_MANUAL_QA_WAIT_MARKERS = (
    "manual qa", "owner qa", "qa confirmation", "manual confirmation",
    "owner confirmation", "awaiting confirmation", "awaiting qa",
    "waiting for confirmation", "waiting on confirmation",
)
# A file/line reference grounds the recommendation in a concrete location, so we
# only treat the result as an actionable diagnosis when one is present. Matches
# path.ext or path.ext:line tokens (e.g. components/Pay.vue:498, src/index.ts).
_FILE_REF_RE = re.compile(r"[\w./-]+\.[A-Za-z]{1,6}(?::\d+)?")


def _recommends_fix(parsed: dict, analysis: str) -> bool:
    """True when a ready=false developer result identified a CONCRETE, located
    fix but produced no diff - i.e. a diagnosis that was never applied. Conservative
    and text-heuristic like _looks_blocked: requires a strong recommend-intent
    marker AND a file/line reference, and never fires when the run was blocked
    (blocked takes precedence), when status says no-fix, or when the analysis
    contains a genuine no-fix/negation phrase."""
    if _looks_blocked(parsed, analysis):
        return False
    status = str(parsed.get("status", "")).lower()
    if status in ("no_fix", "no_fix_needed", "no-change", "ok", "done"):
        return False
    text = (analysis or "").lower()
    if any(m in text for m in _NO_FIX_MARKERS):
        return False
    if not any(m in text for m in _RECOMMEND_FIX_MARKERS):
        return False
    return bool(_FILE_REF_RE.search(analysis or ""))


def _awaits_manual_qa(parsed: dict, analysis: str) -> bool:
    """True when item is fixed/deployed and remaining work is human QA."""
    if _looks_blocked(parsed, analysis):
        return False
    status = str(parsed.get("status", "")).lower()
    if status in ("manual_qa", "awaiting_manual_qa", "awaiting_qa"):
        return True
    text = (analysis or "").lower()
    return (any(marker in text for marker in _MANUAL_QA_FIXED_MARKERS)
            and any(marker in text for marker in _MANUAL_QA_WAIT_MARKERS))


def _manual_qa_close(conn: psycopg.Connection, cfg, request_id, analysis: str) -> None:
    """Close automation but leave an owner-visible manual QA follow-up."""
    text = ("Manual QA follow-up: this REVIEW item is marked fixed and deployed. "
            f"Please confirm whether owner/manual QA passes. {analysis}")
    status.set_status_for_request(conn, request_id, status.DONE)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.team_id, r.conversation_id "
            "FROM requests r WHERE r.id=%s",
            (request_id,))
        row = cur.fetchone()
        if not row:
            return
        team_id, conv_id = row
        cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s",
                    (request_id,))
        cur.execute(
            "UPDATE jobs SET status='dead', updated_at=now() "
            "WHERE request_id=%s AND status IN ('pending','claimed','running')",
            (request_id,))
        dest = _control_destination(conn, cfg, team_id, conv_id)
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, destination_ref, "
            "idempotency_key, payload) VALUES (%s,%s,'notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (request_id, team_id, dest, f"manual_qa:{request_id}",
             psycopg.types.json.Json({"text": text})))


def _no_fix_close(conn: psycopg.Connection, cfg, request_id, analysis: str,
                  *, blocked: bool = False) -> None:
    """The developer investigated and no fix is warranted (or, when blocked=True,
    could not do the work): close the request and report to the channel - no
    qa/senior, no PR, no fabricated edit."""
    prefix = ("Blocked, couldn't complete the task: " if blocked
              else "Investigated, no fix needed: ")
    status.set_status_for_request(conn, request_id, status.FAILED if blocked else status.NOFIX)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.team_id, r.conversation_id, e.kind "
            "FROM requests r LEFT JOIN events e ON e.id = r.event_id "
            "WHERE r.id=%s",
            (request_id,))
        team_id, conv_id, origin_kind = cur.fetchone()
        cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s", (request_id,))
        text = f"{prefix}{analysis}"
        if origin_kind == "signal":
            alerts.record(conn, severity="warn" if blocked else "info",
                          project=str(team_id),
                          fingerprint=f"pipeline-{'blocked' if blocked else 'nofix'}:{request_id}",
                          message=text, channel="log",
                          payload={"request_id": str(request_id)})
            return
        # Resolve the real channel (whatsapp:<jid> for a converse request, else
        # the team control channel). A bare 'conv:<uuid>' is NOT a routable
        # channel - deliver() drops it - so the owner never saw the close/blocked
        # note even though the action was marked done.
        dest = _control_destination(conn, cfg, team_id, conv_id)
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, destination_ref, "
            "idempotency_key, payload) VALUES (%s,%s,'notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (request_id, team_id, dest, f"nofix:{request_id}",
             psycopg.types.json.Json({"text": text})))


def _advance_or_done(conn: psycopg.Connection, cfg, job: Job, team) -> None:
    """Advance a judge stage (qa / browser_verify) on pass: enqueue the next stage,
    or if this was the last stage, approve and record the open_pr action. Advancing
    to the *next* stage (not a hardcoded 'senior') is what lets a browser_verify
    stage sit between qa and senior; for a plain developer->qa->senior pipeline the
    next stage after qa is still senior, so behavior is unchanged."""
    next_index = job.stage + 1
    if next_index >= len(team.pipeline.stages):
        _approve_done(conn, cfg, job)
    else:
        _next(conn, cfg, job.request_id, next_index)


def _next(conn: psycopg.Connection, cfg, request_id: str, stage_index: int) -> None:
    """Advance to stage_index; if out of range, mark done."""
    team_id = _request_team(conn, request_id)
    team = cfg.team(team_id)
    if stage_index >= len(team.pipeline.stages):
        with conn.cursor() as cur:
            cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s",
                        (request_id,))
        log.info("request %s done team=%s", request_id, team_id,
                 extra={"team_id": team_id, "request_id": request_id})
    else:
        enqueue_stage(conn, cfg, request_id=request_id, stage_index=stage_index)


def _loop_back(conn: psycopg.Connection, cfg, job: Job, team, to_role: str,
               detail: str = "") -> None:
    """Loop back to to_role, bumping the branch counter. If over max_iters, fail."""
    to_idx = _index(team, to_role)
    if to_idx is None:
        _fail(conn, cfg, job.request_id, f"Cannot route {job.role} failure back to {to_role}.")
        return
    with conn.cursor() as cur:
        cur.execute("SELECT branch_counters FROM requests WHERE id=%s", (job.request_id,))
        counters = dict(cur.fetchone()[0] or {})
    key = str(to_idx)
    current = int(counters.get(key, 0))
    if current >= team.pipeline.max_iters:
        reason = f"{job.role} did not pass after {team.pipeline.max_iters} rework attempt(s)."
        if detail:
            reason = f"{reason} Blocking issue: {detail}"
        if _review_failure_superseded(conn, job.request_id, job.role):
            log.info("request %s ignored stale %s failure after later review pass",
                     job.request_id, job.role,
                     extra={"team_id": job.team_id, "request_id": job.request_id,
                            "job_id": job.id})
            return
        reason = _classified_failure_note(reason)
        _record_memory_outcome(
            conn, job.request_id, "qa-fail",
            reason,
        )
        if _open_draft_pr_after_failure(conn, cfg, job, reason):
            return
        _fail(conn, cfg, job.request_id, reason)
        return
    counters[key] = current + 1
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET branch_counters=%s, updated_at=now() WHERE id=%s",
                    (Json(counters), job.request_id))
    enqueue_stage(conn, cfg, request_id=job.request_id, stage_index=to_idx)


def _handle_recommended_fix(conn: psycopg.Connection, cfg, job: Job, team,
                            analysis: str) -> None:
    """The developer diagnosed a concrete, located fix but produced no diff.
    Re-dispatch it once (bounded by the same branch-counter/max_iters budget as
    _loop_back) with an augmented brief that seeds the prior analysis and tells it
    to APPLY the fix - converting a diagnosis into a fix attempt. When the budget
    is exhausted (still no diff after retry), surface a distinct, visible
    'found, not fixed' outcome instead of a clean 'no fix needed' close."""
    dev_idx = _index(team, "developer")
    if dev_idx is None:
        dev_idx = job.stage
    # Deliberately shares the developer-index branch_counters slot with _loop_back
    # (qa/senior rework). One budget bounds total developer re-dispatches per
    # request regardless of why, so a recommend-fix retry and a qa-fail rework
    # can't together exceed max_iters. Do not split this into a separate counter.
    with conn.cursor() as cur:
        cur.execute("SELECT branch_counters FROM requests WHERE id=%s", (job.request_id,))
        counters = dict(cur.fetchone()[0] or {})
    key = str(dev_idx)
    current = int(counters.get(key, 0))
    if current >= team.pipeline.max_iters:
        # Budget exhausted: a real fix was found but never applied. Surface it.
        _record_memory_outcome(conn, job.request_id, "found-not-fixed", analysis)
        _found_not_fixed_close(conn, cfg, job.request_id, analysis)
        return
    # Re-dispatch the developer with the prior diagnosis seeded so it applies the
    # fix instead of re-investigating from scratch (mirrors _handle_research's
    # brief seeding).
    seed = ("A previous developer pass already diagnosed this and located the fix "
            "but did NOT apply it. Apply the fix it identified now and produce a "
            "diff. Do NOT re-investigate from scratch.\n\n"
            f"Prior diagnosis:\n{analysis}")
    _seed_event_text(conn, job.event_id, seed)
    counters[key] = current + 1
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET branch_counters=%s, updated_at=now() WHERE id=%s",
                    (Json(counters), job.request_id))
    enqueue_stage(conn, cfg, request_id=job.request_id, stage_index=dev_idx)


def _found_not_fixed_close(conn: psycopg.Connection, cfg, request_id,
                           analysis: str) -> None:
    """Distinct terminal outcome for a diagnosed-but-unapplied fix: the developer
    found the root cause and a concrete fix but, even after a bounded retry, never
    produced a diff. This is NOT 'no fix needed' - mark the request FAILED and
    raise a WARN-severity alert that says a fix is outstanding, so it is visibly
    different from a clean no-fix resolve and gets owner attention."""
    text = ("Found root cause but no fix was applied - needs attention. "
            f"{analysis}")
    status.set_status_for_request(conn, request_id, status.FAILED)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.team_id, r.conversation_id, e.kind "
            "FROM requests r LEFT JOIN events e ON e.id = r.event_id "
            "WHERE r.id=%s",
            (request_id,))
        team_id, conv_id, origin_kind = cur.fetchone()
        cur.execute("UPDATE requests SET status='failed', updated_at=now() WHERE id=%s",
                    (request_id,))
        # Cancel non-terminal sibling jobs in the same transaction, same as _fail:
        # this terminal flips the request to 'failed', after which on_job_done
        # early-returns and jobs.claim never re-checks parent status, so any
        # pending/claimed/running sibling would otherwise zombie. (Dev-stage-0 has
        # none today, but this keeps the invariant if the path is reached later.)
        cur.execute(
            "UPDATE jobs SET status='dead', updated_at=now() "
            "WHERE request_id=%s AND status IN ('pending','claimed','running')",
            (request_id,))
        if origin_kind == "signal":
            alerts.record(conn, severity="warn", project=str(team_id),
                          fingerprint=f"pipeline-found-not-fixed:{request_id}",
                          message=text, channel="log",
                          payload={"request_id": str(request_id)})
            return
        dest = _control_destination(conn, cfg, team_id, conv_id)
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, destination_ref, "
            "idempotency_key, payload) VALUES (%s,%s,'notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (request_id, team_id, dest, f"found_not_fixed:{request_id}",
             Json({"text": text})))


def _fail(conn: psycopg.Connection, cfg, request_id: str, reason: str) -> None:
    status.set_status_for_request(conn, request_id, status.FAILED)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.team_id, r.conversation_id, e.kind "
            "FROM requests r LEFT JOIN events e ON e.id = r.event_id "
            "WHERE r.id=%s",
            (request_id,))
        row = cur.fetchone()
        if not row:
            return
        team_id, conv_id, origin_kind = row
        cur.execute("UPDATE requests SET status='failed', updated_at=now() WHERE id=%s",
                    (request_id,))
        log.info("request %s failed team=%s reason=%s", request_id, team_id, reason,
                 extra={"team_id": team_id, "request_id": str(request_id)})
        # Cancel sibling jobs in the same transaction. on_job_done early-returns
        # once the request is not 'open', and jobs.claim filters only on
        # status='pending' (never parent-request status), so any non-terminal
        # sibling would otherwise linger as a zombie 'pending' job forever.
        # 'dead' is the terminal-cancel state ('cancelled' is not in the jobs
        # CHECK constraint). Cancelling 'claimed'/'running' siblings is safe:
        # a worker's finalize/heartbeat is a fenced CAS on
        # status IN ('claimed','running'), so once 'dead' the worker is fenced
        # out and cannot resurrect the job. Do not add 'dead' to that CAS set.
        cur.execute(
            "UPDATE jobs SET status='dead', updated_at=now() "
            "WHERE request_id=%s AND status IN ('pending','claimed','running')",
            (request_id,))
        text = _failure_text(conn, request_id, reason)
        if origin_kind == "signal":
            # Auto/signal-origin request: nobody is waiting on it, so don't ping
            # the owner. Log it instead; dashboard and digests still surface it.
            alerts.record(conn, severity="warn", project=str(team_id),
                          fingerprint=f"pipeline-failed:{request_id}",
                          message=text, channel="log")
            return
        dest = _control_destination(conn, cfg, team_id, conv_id)
        cur.execute(
            "INSERT INTO actions (request_id, team_id, type, risk, destination_ref, "
            "idempotency_key, payload) VALUES (%s,%s,'notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (request_id, team_id, dest, f"pipeline_failed:{request_id}",
             Json({"text": text})))


def _approve_done(conn: psycopg.Connection, cfg, job: Job) -> None:
    """Senior approved. Record a ready open_pr action with useful context, then
    mark the request done. Executor runs push + gh pr create via injectable runner."""
    from argus.v2.workspace import repo as workspace
    team = cfg.team(job.team_id)
    project = getattr(team, "project", None)
    with conn.cursor() as cur:
        if project and job.request_id:
            branch = f"{project.work_branch_prefix}/{job.request_id}"
            cwd = str(workspace._wt_path(job.request_id))
            try:
                diff_text = workspace.diff(project, cwd)
            except Exception:
                diff_text = ""
            scan = pm_scan.scan_diff(diff_text)
            if pm_scan.has_critical(scan):
                reason = _classified_failure_note(
                    f"Deterministic diff scan blocked PR: {pm_scan.format_findings(scan)}"
                )
                _record_memory_outcome(
                    conn, job.request_id, "qa-fail",
                    reason,
                )
                _fail(conn, cfg, job.request_id, reason)
                return
            pr_info = _pr_info(conn, cfg, job.request_id, cwd=cwd)
            _record_memory_outcome(conn, job.request_id, "qa-pass", pr_info["summary_short"])
            cur.execute(
                "INSERT INTO actions "
                "(request_id, job_id, team_id, type, risk, "
                " destination_ref, idempotency_key, payload) "
                "VALUES (%s,%s,%s,'open_pr','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (
                    job.request_id,
                    job.id,
                    job.team_id,
                    _control_destination(conn, cfg, job.team_id, job.conversation_id),
                    f"open_pr:{job.request_id}",
                    Json({"branch": branch,
                          "base": project.base_branch,
                          "remote": project.remote,
                          "draft": project.autofix.draft,
                          "title": pr_info["title"],
                          "body": pr_info["body"],
                          "request": pr_info["request"],
                          "summary_short": pr_info["summary_short"],
                          "checks": pr_info["checks"],
                          "risk_summary": pr_info["risk_summary"],
                          "changed_files": pr_info["changed_files"],
                          "cwd": cwd}),
                ),
            )
        cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s",
                    (job.request_id,))
    log.info("request %s done (senior approved) team=%s job=%s",
             job.request_id, job.team_id, job.id,
             extra={"job_id": job.id, "team_id": job.team_id,
                    "request_id": job.request_id})
    status.set_status(conn, job.event_id, status.DONE)


def _open_draft_pr_after_failure(conn: psycopg.Connection, cfg, job: Job, reason: str) -> bool:
    from argus.v2.workspace import repo as workspace

    team = cfg.team(job.team_id)
    project = getattr(team, "project", None)
    if not project or not getattr(project.autofix, "force_draft_on_fail", False):
        return False
    if not _latest_builder_has_diff(conn, job.request_id):
        return False
    branch = f"{project.work_branch_prefix}/{job.request_id}"
    cwd = str(workspace._wt_path(job.request_id))
    try:
        diff_text = workspace.diff(project, cwd)
    except Exception:
        diff_text = ""
    scan = pm_scan.scan_diff(diff_text)
    if pm_scan.has_critical(scan):
        reason = _classified_failure_note(
            f"Deterministic diff scan blocked PR: {pm_scan.format_findings(scan)}"
        )
        _record_memory_outcome(
            conn, job.request_id, "qa-fail",
            reason,
        )
        _fail(conn, cfg, job.request_id, reason)
        return True

    failure_label = "QA failed" if job.role == "qa" else f"{job.role} failed"
    classification = _failure_classification(reason)
    risk = (f"needs review: {failure_label}; failure classification: {classification}; "
            f"{reason} Opened as draft so the diff is inspectable.")
    pr_info = _pr_info(conn, cfg, job.request_id, cwd=cwd, risk_summary=risk)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions "
            "(request_id, job_id, team_id, type, risk, "
            " destination_ref, idempotency_key, payload) "
            "VALUES (%s,%s,%s,'open_pr','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (
                job.request_id,
                job.id,
                job.team_id,
                _control_destination(conn, cfg, job.team_id, job.conversation_id),
                f"open_pr:{job.request_id}",
                Json({"branch": branch,
                      "base": project.base_branch,
                      "remote": project.remote,
                      "draft": True,
                      "title": pr_info["title"],
                      "body": pr_info["body"],
                      "request": pr_info["request"],
                      "summary_short": pr_info["summary_short"],
                      "checks": pr_info["checks"],
                      "risk_summary": pr_info["risk_summary"],
                      "changed_files": pr_info["changed_files"],
                      "cwd": cwd}),
            ),
        )
        cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s",
                    (job.request_id,))
    return True


def _record_memory_outcome(conn: psycopg.Connection, request_id: str,
                           outcome: str, note: str) -> None:
    try:
        pm_memory.record_request_outcome(conn, request_id=request_id,
                                         outcome=outcome,
                                         note=_memory_outcome_note(outcome, note))
    except Exception:
        pass


def _memory_outcome_note(outcome: str, note: str) -> str:
    if outcome == "blocked":
        return f"Blocking issue: {note}"
    if outcome == "found-not-fixed":
        return f"Next repair action: apply the diagnosed fix. Evidence: {note}"
    if outcome == "no-change":
        if _has_known_root_cause(note):
            if "Blocking issue:" in note or "Next repair action:" in note:
                return note
            return f"Next repair action: address the known root cause. Evidence: {note}"
        return f"Next repair action: none, no code change warranted. Evidence: {note}"
    if outcome == "qa-fail":
        note = _classified_failure_note(note)
        if _has_known_root_cause(note):
            if "Blocking issue:" in note or "Next repair action:" in note:
                return note
            return f"Next repair action: address the review failure root cause. Evidence: {note}"
        return note
    return note


def _has_known_root_cause(text: str) -> bool:
    lowered = text.lower()
    return "root cause" in lowered and "no root cause" not in lowered


def _review_failure_superseded(conn: psycopg.Connection, request_id: str,
                               failed_role: str) -> bool:
    """True when a failure callback is older than the latest review result."""
    latest = _latest_review_outcomes(conn, request_id)
    if latest.get("senior") == "approve":
        return True
    if failed_role == "qa" and latest.get("qa") == "pass":
        return True
    if failed_role == "browser_verify" and latest.get("browser_verify") in ("pass", "skipped"):
        return True
    if failed_role == "senior" and latest.get("senior") == "approve":
        return True
    return False


def _latest_review_outcomes(conn: psycopg.Connection, request_id: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (role) role, result
            FROM jobs
            WHERE request_id=%s
              AND kind='pipeline'
              AND status='done'
              AND role IN ('qa', 'browser_verify', 'senior')
            ORDER BY role, created_at DESC, updated_at DESC
            """,
            (request_id,),
        )
        rows = cur.fetchall()
    outcomes: dict[str, str] = {}
    for role, result in rows:
        parsed = (result or {}).get("parsed", {}) if isinstance(result, dict) else {}
        if role == "qa":
            outcomes[role] = contracts.qa_verdict(parsed, (result or {}).get("test_exit"))
        elif role == "browser_verify":
            meta = (result or {}).get("browser_verify", {}) if isinstance(result, dict) else {}
            if isinstance(meta, dict) and meta.get("skipped"):
                outcomes[role] = "skipped"
            else:
                outcomes[role] = str(parsed.get("verdict") or "")
        elif role == "senior":
            outcomes[role] = str(parsed.get("decision") or "")
    return outcomes


def _parsed_failure_detail(parsed: dict) -> str:
    for key in ("reason", "analysis", "summary", "message"):
        value = parsed.get(key)
        if value:
            return str(value)
    return ""


def _classified_failure_note(reason: str) -> str:
    if "failure classification:" in (reason or "").lower():
        return reason
    return f"Failure classification: {_failure_classification(reason)}. {reason}"


def _failure_classification(text: str) -> str:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in (
        "superseded", "expected cancellation", "expected cancelation",
        "cancelled because", "canceled because", "cancelled by newer",
        "canceled by newer", "newer request",
    )):
        return "expected cancellation"
    if any(marker in lowered for marker in (
        "stale status", "stale deployment", "deployment is stale",
        "outdated deployment", "newer deployment",
    )):
        return "stale status"
    if any(marker in lowered for marker in (
        "sandbox networking", "network disabled", "network is disabled",
        "network access", "connection refused", "could not resolve",
        "temporary failure in name resolution", "localhost", "127.0.0.1",
        "postgres", "pypi", "http 403", "403", "forbidden", "token 401",
        "401", "unauthorized", "not authorized",
    )):
        return "environment blocker"
    if any(marker in lowered for marker in (
        "code regression", "regression", "test failed", "tests failed",
        "failed tests", "assertionerror", "traceback", "typeerror",
        "valueerror", "deterministic diff scan",
    )):
        return "code regression"
    return "unknown"


def _pr_info(conn: psycopg.Connection, cfg, request_id: str, *, cwd: str,
             risk_summary: str | None = None) -> dict:
    request = _request_text_for_request(conn, request_id)
    files = _changed_files(cwd)
    checks = _checks_summary(conn, request_id)
    batch_count = _batch_bug_count(conn, request_id)
    title = (f"Argus: Investigate {batch_count} open bug reports" if batch_count
             else _title(request, request_id))
    summary = _builder_summary(conn, request_id) or _summary_short(request)
    risk = risk_summary or "low: QA passed and senior approved"
    body = "\n".join([
        f"## Request\n{request or request_id}",
        "",
        f"## Summary\n{summary}",
        "",
        f"## Status\n{_pr_status(risk)}",
        "",
        f"## Verification\n{_checks_markdown(checks)}",
        "",
        "## Changed Files",
        _bullet_list(files) if files else "- No file list available",
        "",
        f"## Risk / Next Step\n{_risk_markdown(risk)}",
        "",
        f"Generated by Argus request `{request_id}`.",
    ])
    return {
        "title": title,
        "request": request,
        "summary_short": summary,
        "body": body,
        "checks": checks,
        "risk_summary": risk,
        "changed_files": files,
    }


def _latest_builder_has_diff(conn: psycopg.Connection, request_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result FROM jobs WHERE request_id=%s AND result ? 'has_diff' "
            "ORDER BY updated_at DESC LIMIT 1",
            (request_id,))
        row = cur.fetchone()
    return bool(row and isinstance(row[0], dict) and row[0].get("has_diff") is True)


def _request_text_for_request(conn: psycopg.Connection, request_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT event_id FROM requests WHERE id=%s", (request_id,))
        row = cur.fetchone()
    return _request_text(conn, row[0]) if row else ""


def _batch_bug_count(conn: psycopg.Connection, request_id: str) -> int:
    """Number of bugs in this request's originating batch signal, or 0 if the
    request did not originate from a batched supabase bug_batch signal. Drives
    the fixed 'Argus: Investigate N open bug reports' PR title (see _pr_info)
    instead of the generic first-72-chars title, which would otherwise
    truncate mid-sentence for a long combined bug list."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.payload FROM requests r JOIN events e ON e.id = r.event_id "
            "WHERE r.id=%s AND e.kind='signal'",
            (request_id,))
        row = cur.fetchone()
    if not row or not isinstance(row[0], dict):
        return 0
    payload = row[0]
    if payload.get("kind") != "bug_batch":
        return 0
    rows = payload.get("rows")
    return len(rows) if isinstance(rows, list) else 0


def _checks_summary(conn: psycopg.Connection, request_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, result FROM jobs WHERE request_id=%s "
            "ORDER BY stage, updated_at",
            (request_id,))
        rows = cur.fetchall()
    order = []
    parts = {}
    for role, result in rows:
        parsed = (result or {}).get("parsed", {}) if isinstance(result, dict) else {}
        part = ""
        if role == "qa":
            verdict = contracts.qa_verdict(parsed, (result or {}).get("test_exit"))
            part = f"QA: {verdict}"
            if verdict != "pass":
                part += f" ({_classified_failure_note(_result_failure_text(role, result))})"
        elif role == "browser_verify":
            meta = (result or {}).get("browser_verify", {}) if isinstance(result, dict) else {}
            if isinstance(meta, dict) and meta.get("skipped"):
                part = "Browser: skipped (no UI files changed)"
            else:
                verdict = parsed.get("verdict") or "skip"
                part = f"Browser: {verdict}"
                if verdict != "pass":
                    part += f" ({_classified_failure_note(_result_failure_text(role, result))})"
        elif role == "senior":
            decision = parsed.get("decision") or "no decision"
            part = f"Senior: {decision}"
            if decision != "approve":
                part += f" ({_classified_failure_note(_result_failure_text(role, result))})"
        if part:
            if role not in parts:
                order.append(role)
            parts[role] = part
    return "; ".join(parts[role] for role in order) or "QA and senior approved"


def _result_failure_text(role: str, result) -> str:
    if not isinstance(result, dict):
        return str(role)
    parsed = result.get("parsed")
    details = []
    if isinstance(parsed, dict):
        details.append(_parsed_failure_detail(parsed))
    for key in ("test_output", "output", "error"):
        value = result.get(key)
        if value:
            details.append(str(value))
    return " ".join(part for part in details if part) or str(role)


def _changed_files(cwd: str) -> list[str]:
    commands = [
        ["git", "diff", "--name-only", "HEAD~1..HEAD"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "status", "--short"],
    ]
    for argv in commands:
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            files = []
            for line in proc.stdout.splitlines():
                name = line.strip()
                if argv[:2] == ["git", "status"]:
                    name = name[3:].strip() if len(name) > 3 else name
                if name:
                    files.append(name)
            if files:
                return files[:20]
    return []


def _title(request: str, request_id: str) -> str:
    text = " ".join((request or "").split())
    if not text:
        return f"Argus: {request_id}"
    return f"Argus: {text[:72]}"


def _builder_summary(conn: psycopg.Connection, request_id: str) -> str:
    """The builder's own LLM summary of what was wrong and what it changed
    (ARGUS_RESULT summary/analysis). This is what the owner sees in the PR and
    the WhatsApp digest, and what feeds project memory - never a mechanical
    filename list. The builder job is the one that recorded has_diff."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT result FROM jobs WHERE request_id=%s AND result ? 'has_diff' "
            "ORDER BY updated_at DESC LIMIT 1",
            (request_id,))
        row = cur.fetchone()
    if not row or not isinstance(row[0], dict):
        return ""
    parsed = row[0].get("parsed")
    if not isinstance(parsed, dict):
        return ""
    text = str(parsed.get("summary") or parsed.get("analysis") or "").strip()
    if not text:
        text = _builder_output_summary(str(row[0].get("output") or ""))
    return " ".join(text.split())[:600]


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BUILDER_FILE_REF_RE = re.compile(r"\s+in\s+[\w./-]+\.[A-Za-z]{1,8}(?::\d+)?")


def _builder_output_summary(output: str) -> str:
    text = _MD_LINK_RE.sub(r"\1", output or "")
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        lower = line.lower()
        if not lower.startswith(("fix:", "smallest fix", "smallest safe fix", "change:")):
            continue
        inline = line.split(":", 1)[1].strip() if ":" in line else ""
        if inline and len(inline.split()) > 3:
            return _clean_builder_summary(inline)
        parts = []
        for item in lines[index + 1:]:
            if not item:
                if parts:
                    break
                continue
            if item.lower().startswith(("verification", "verified", "notes", "risk", "pr readiness")):
                break
            parts.append(item.lstrip("- ").strip())
            if len(" ".join(parts)) >= 240:
                break
        if parts:
            return _clean_builder_summary(" ".join(parts))
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return _clean_builder_summary(paragraphs[0]) if paragraphs else ""


def _clean_builder_summary(text: str) -> str:
    text = _BUILDER_FILE_REF_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if "." in text:
        text = text.split(".", 1)[0].strip() + "."
    return text


def _summary_short(request: str) -> str:
    # Fallback only: used when the builder emitted no LLM summary. Plain request
    # text, no mechanical filename string (the owner asked for issue+fix, not paths).
    return " ".join((request or "Automated change").split())[:160]


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"- `{item}`" for item in items)


def _checks_markdown(checks: str) -> str:
    lines = [part.strip() for part in str(checks or "").split(";") if part.strip()]
    if not lines:
        return "- No check summary recorded"
    return "\n".join(f"- {line}" for line in lines)


def _risk_markdown(risk: str) -> str:
    lines = _risk_lines(risk)
    if not lines:
        return "- No review blockers detected."
    return "\n".join(f"- {line}" for line in lines)


def _risk_lines(risk: str) -> list[str]:
    text = _clean_risk_text(risk)
    if not text or text.lower().startswith("low"):
        return []
    return [_short_text(part, 260)
            for part in (piece.strip() for piece in text.split(";"))
            if part]


def _pr_status(risk: str) -> str:
    lowered = str(risk or "").lower()
    if not lowered or lowered.startswith("low"):
        return "Checked and verified"
    if "browser_verify" in lowered or "browser verify" in lowered:
        return "Needs review: browser verification failed"
    if "senior failed" in lowered:
        return "Needs review: senior did not approve"
    if "qa failed" in lowered:
        return "Needs review: QA failed"
    return "Needs review"


def _clean_risk_text(risk: str) -> str:
    text = " ".join(str(risk or "").split())
    return re.sub(r"Command \[.*\] timed out after ([0-9.]+) seconds",
                  r"browser command timed out after \1 seconds", text)


def _short_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _failure_text(conn: psycopg.Connection, request_id: str, reason: str) -> str:
    request = _request_text_for_request(conn, request_id)
    detail = _last_failure_detail(conn, request_id)
    classification = _failure_classification(f"{reason}\n{detail}")
    next_line = "Next: fix failing stage and rerun request."
    if _may_have_partial_side_effects(f"{reason}\n{detail}"):
        next_line = ("Next: inspect live state and job transcript before rerun; "
                     "timeout or outage may have left external side effects.")
    lines = [
        "Argus pipeline stopped before normal completion.",
        f"Request: {request or request_id}",
        f"Failure classification: {classification}",
        f"Reason: {reason}",
        next_line,
    ]
    if detail:
        lines += ["", "Last failure:", detail]
    return "\n".join(lines)


def _may_have_partial_side_effects(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in (
        "process timed out",
        "timed out after",
        "timeout",
        "deadline_exceeded",
        "outage",
    ))


def _last_failure_detail(conn: psycopg.Connection, request_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, result FROM jobs WHERE request_id=%s "
            "ORDER BY updated_at DESC LIMIT 1",
            (request_id,))
        row = cur.fetchone()
    if not row:
        return ""
    role, result = row
    if isinstance(result, dict):
        output = str(result.get("test_output") or result.get("output") or result.get("error") or "")
    else:
        output = ""
    output = output.strip()
    if len(output) > 1200:
        output = output[-1200:]
    return f"{role}: {output}" if output else str(role)


def _control_destination(conn: psycopg.Connection, cfg, team_id: str,
                         conversation_id=None) -> str:
    if conversation_id:
        with conn.cursor() as cur:
            cur.execute("SELECT channel_ref FROM conversations WHERE id=%s",
                        (conversation_id,))
            row = cur.fetchone()
        if row and row[0]:
            return row[0]
    try:
        team = cfg.team(team_id)
    except KeyError:
        return "cli:local"
    for channel in team.channels:
        if channel.role == "control" and channel.type != "cli":
            return f"{channel.type}:{channel.channel_id}"
    return "cli:local"


def _is_qa(team, role_name: str) -> bool:
    """True if role_name is the qa stage in this team's pipeline."""
    try:
        role = team.role(role_name)
        return role.kind == "judge" and role_name == "qa"
    except KeyError:
        return False


def _is_browser_verify(team, role_name: str) -> bool:
    """True if role_name is the browser_verify judge stage."""
    try:
        role = team.role(role_name)
        return role.kind == "judge" and role_name == "browser_verify"
    except KeyError:
        return False


def _request_team(conn: psycopg.Connection, request_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT team_id FROM requests WHERE id=%s", (request_id,))
        return cur.fetchone()[0]


def _request_text(conn: psycopg.Connection, event_id) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT kind, source, payload FROM events WHERE id=%s", (event_id,))
        row = cur.fetchone()
    if not row:
        return ""
    kind, source, payload = row[0], row[1], (row[2] or {})
    if payload.get("text"):
        return payload["text"]
    if kind == "signal":
        if payload.get("kind") == "bug_batch" and payload.get("rows"):
            return _batch_signal_text(source, payload)
        import json
        return (f"An automated signal arrived from source '{source}'. Investigate it "
                f"and make a minimal, focused fix if one is warranted.\n\nSignal details:\n"
                f"{json.dumps(payload, indent=2, default=str)}")
    return ""


def _batch_signal_text(source: str, payload: dict) -> str:
    """Request text for a consolidated bug_batch signal (see the supabase
    connector's batch mode). Starts with 'Investigate N open bug reports' so
    _title() (first 72 chars, 'Argus: ' prefix) turns it into exactly the PR
    title the owner asked for, with the full bug list in the body below."""
    rows = payload.get("rows") or []
    lines = [
        f"Investigate {len(rows)} open bug reports",
        "",
        f"Source: '{source}'. For each bug below, find the root cause and make a "
        "minimal, focused fix if one is warranted. Land all fixes as ONE combined "
        "PR, not one PR per bug.",
        "",
        "Bugs:",
    ]
    for finding in rows:
        row = finding.get("row") or {}
        bug_id = row.get("id", "?")
        title = finding.get("message") or "(no title)"
        severity = finding.get("severity") or "warn"
        lines.append(f"- id={bug_id} severity={severity}: {title}")
    return "\n".join(lines)


def _config_hash(cfg) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()[:16]


def _add_prompt_hash(snapshot: dict) -> None:
    """Hash the fully-assembled prompt, exactly what
    build_prompt hands the worker) so a regression can be correlated with a
    prompt, rules, skills, or checkpoints edit later. Call after those blocks
    are already frozen into the snapshot."""
    assembled = "\n\n".join(
        part for part in (snapshot.get("prompt", ""), snapshot.get("rules", ""),
                          snapshot.get("skills", ""), snapshot.get("checkpoints", ""))
        if part
    )
    snapshot["prompt_hash"] = hashlib.sha256(assembled.encode()).hexdigest()[:12]


def _add_pipeline_checkpoints(snapshot: dict) -> None:
    snapshot["checkpoints"] = _PIPELINE_CHECKPOINTS


def _add_manager_checkpoints(snapshot: dict) -> None:
    snapshot["checkpoints"] = _MANAGER_CHECKPOINTS


def _add_skills(snapshot: dict, role_name: str, allow, text: str) -> None:
    """Freeze the rendered load-on-relevance skills block into the snapshot so
    the job replays deterministically. No-op (no key added) when nothing matches,
    keeping the prompt byte-identical to pre-skills for empty pools/configs."""
    block = skills.block_for(role_name, text, allow=(allow or None))
    if block:
        snapshot["skills"] = block


def _add_rules(conn: psycopg.Connection, cfg, snapshot: dict, team_id: str) -> None:
    block = rules_context.block_for(conn, cfg, team_id=team_id)
    if block:
        snapshot["rules"] = block


def enqueue_converse(conn: psycopg.Connection, cfg, *, event_id: str,
                     team_id: str, conversation_id: Optional[str]) -> str:
    """Enqueue a converse job for the team's manager role. Idempotent on
    event_id (key 'converse:<event_id>'). Returns the job id."""
    team = cfg.team(team_id)
    role = team.role("manager")
    eng = loader.resolve_engine(cfg, team_id, "manager")
    snapshot: dict = {
        "engine": eng.engine,
        "model": eng.model,
        "prompt": role.prompt,
        "config_hash": _config_hash(cfg),
        # Selects the hermes per-project learning profile (HERMES_HOME), so the
        # manager accumulates memory across conversations. No-op for codex/echo.
        "project": team_id,
    }
    snapshot.update(_role_snapshot_extra("manager"))
    # Read event text to carry in the payload.
    text = _request_text(conn, event_id)
    _add_rules(conn, cfg, snapshot, team_id)
    _add_skills(snapshot, "manager", role.skills, text)
    _add_manager_checkpoints(snapshot)
    _add_prompt_hash(snapshot)
    return jobs.enqueue(
        conn,
        team_id=team_id,
        kind="converse",
        role="manager",
        stage=0,
        idempotency_key=f"converse:{event_id}",
        exec_snapshot=snapshot,
        payload={"text": text},
        request_id=None,
        event_id=event_id,
        conversation_id=conversation_id,
    )


def enqueue_triage(conn: psycopg.Connection, cfg, *, event_id: str,
                   team_id: str, fingerprint: Optional[str]) -> str:
    """Enqueue a manager TRIAGE job for a monitoring signal. The manager decides
    investigate / dispatch / ignore (it does not blindly open a code fix). No
    worktree (decides from the signal + team state). Idempotent per fingerprint
    so a re-emitted signal is not re-triaged while one is in flight."""
    team = cfg.team(team_id)
    role = team.role("manager")
    eng = loader.resolve_engine(cfg, team_id, "manager")
    text = _signal_task_text(conn, event_id, mode="triage")
    snapshot: dict = {"engine": eng.engine, "model": eng.model, "prompt": role.prompt,
                      "config_hash": _config_hash(cfg), "project": team_id}
    _add_rules(conn, cfg, snapshot, team_id)
    _add_skills(snapshot, "manager", role.skills, text)
    _add_manager_checkpoints(snapshot)
    _add_prompt_hash(snapshot)
    snapshot.update(_role_snapshot_extra("manager"))
    key = f"triage:{team_id}:{fingerprint or event_id}"
    return jobs.enqueue(conn, team_id=team_id, kind="triage", role="manager", stage=0,
                        idempotency_key=key, exec_snapshot=snapshot,
                        payload={"text": text, "fingerprint": fingerprint},
                        request_id=None, event_id=event_id, conversation_id=None)


def enqueue_research(conn: psycopg.Connection, cfg, *, event_id: str,
                     team_id: str, fingerprint: Optional[str]) -> str:
    """Enqueue a read-only RESEARCH job for the team's researcher role. Gets a
    worktree (request_id=event_id) so it can read the repo; it commits nothing
    and returns a structured brief the developer reuses. Falls back to a direct
    dispatch if the team has no researcher role."""
    team = cfg.team(team_id)
    try:
        role = team.role("researcher")
    except KeyError:
        # No researcher configured: act on the manager's dispatch directly.
        return open_request(conn, cfg, event_id=event_id, team_id=team_id,
                            conversation_id=None, fingerprint=fingerprint or event_id,
                            dedup_terminal=True) or ""
    eng = loader.resolve_engine(cfg, team_id, "researcher")
    text = _signal_task_text(conn, event_id, mode="research")
    snapshot: dict = {"engine": eng.engine, "model": eng.model, "prompt": role.prompt,
                      "config_hash": _config_hash(cfg), "project": team_id}
    _add_rules(conn, cfg, snapshot, team_id)
    _add_skills(snapshot, "researcher", role.skills, text)
    _add_prompt_hash(snapshot)
    snapshot.update(_role_snapshot_extra("researcher"))
    key = f"research:{team_id}:{fingerprint or event_id}"
    # request_id stays NULL (FK -> requests); the worker creates a read-only
    # worktree keyed by event_id for research jobs (see worker.run_once).
    return jobs.enqueue(conn, team_id=team_id, kind="research", role="researcher", stage=0,
                        idempotency_key=key, exec_snapshot=snapshot,
                        payload={"text": text, "fingerprint": fingerprint},
                        request_id=None, event_id=event_id, conversation_id=None)


def _signal_task_text(conn: psycopg.Connection, event_id, mode: str) -> str:
    """Frame a signal event for the manager (triage) or researcher (investigate).
    Builds on _request_text (which already renders the signal payload)."""
    base = _request_text(conn, event_id)
    if mode == "triage":
        return ("A monitoring signal arrived. Triage it and decide exactly one "
                "action: investigate (ask the researcher to look first when the "
                "cause/owner is unclear or it spans multiple files), dispatch (the "
                "signal already pinpoints a clear, single fix), or ignore (noise / "
                "expected / not actionable).\n\n" + base)
    return ("Investigate this signal read-only: find the root cause, do NOT change "
            "any files, and report a short structured brief.\n\n" + base)


def _handle_triage(conn: psycopg.Connection, cfg, job: Job) -> None:
    """Act on a completed manager triage job. Marker action keyed 'triage:<job>'
    makes re-sweep safe. investigate -> research; dispatch -> open request; ignore
    -> record."""
    fp = (job.payload or {}).get("fingerprint") or f"triage:{job.id}"
    parsed = _job_parsed(conn, job.id)
    action, task = contracts.triage_decision(parsed)
    reply = str(parsed.get("reply") or "").strip()
    if job.status != "done":
        action = "ignore"  # manager engine failed: hold, the signal re-emits later
        reply = ""
    if action == "dispatch":
        _seed_event_text(conn, job.event_id, task)
        open_request(conn, cfg, event_id=job.event_id, team_id=job.team_id,
                     conversation_id=None, fingerprint=fp, dedup_terminal=True)
    elif action == "investigate":
        enqueue_research(conn, cfg, event_id=job.event_id, team_id=job.team_id,
                         fingerprint=fp)
    _triage_marker(conn, cfg, job, f"triage:{job.id}", note=action, text=reply)


def _handle_research(conn: psycopg.Connection, cfg, job: Job) -> None:
    """Act on a completed researcher job. recommend=fix -> open a dev request
    seeded with the brief (so the developer does not re-investigate); no_fix ->
    record and stop."""
    fp = (job.payload or {}).get("fingerprint") or f"research:{job.id}"
    parsed = _job_parsed(conn, job.id)
    recommend, brief = contracts.research_decision(parsed)
    if job.status != "done":
        recommend = "no_fix"
    if recommend == "fix":
        seed = (f"A researcher already investigated this signal. Use their brief; "
                f"do NOT re-investigate, go straight to the minimal fix.\n\n"
                f"Research brief:\n{brief}" if brief else None)
        _seed_event_text(conn, job.event_id, seed)
        open_request(conn, cfg, event_id=job.event_id, team_id=job.team_id,
                     conversation_id=None, fingerprint=fp, dedup_terminal=True)
    text = brief if recommend == "no_fix" else ""
    _triage_marker(conn, cfg, job, f"research:{job.id}", note=recommend, text=text)


def _job_parsed(conn: psycopg.Connection, job_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM jobs WHERE id=%s", (job_id,))
        row = cur.fetchone()
    return ((row[0] if row else None) or {}).get("parsed") or {}


def _seed_event_text(conn: psycopg.Connection, event_id, text: Optional[str]) -> None:
    """Overwrite the source event's text so the opened request carries the
    manager task / researcher brief. No-op when text is empty."""
    if not text or not event_id:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE events SET payload=jsonb_set(payload, '{text}', "
                    "to_jsonb(%s::text)) WHERE id=%s", (text, event_id))


def _triage_marker(conn: psycopg.Connection, cfg, job: Job, idem: str, *,
                   note: str, text: str = "") -> None:
    """Idempotent marker for triage/research jobs, optionally owner-visible."""
    payload = {"triage": note}
    status = "done"
    destination_ref = None
    if text:
        payload["text"] = text
        status = "proposed"
        destination_ref = _control_destination(conn, cfg, job.team_id, job.conversation_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (job_id, team_id, type, risk, destination_ref, "
            "  idempotency_key, status, payload) "
            "VALUES (%s,%s,'notify','reversible_internal',%s,%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (job.id, job.team_id, destination_ref, idem, status, Json(payload)))


def _handle_converse(conn: psycopg.Connection, cfg, job: Job) -> None:
    """Act on a completed converse job. Guarded by a marker action keyed
    'converse:<job_id>' so re-sweep is safe."""
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM jobs WHERE id=%s", (job.id,))
        row = cur.fetchone()
    if not row:
        return
    result = row[0] or {}
    parsed = result.get("parsed") or {}

    action, reply, task = contracts.converse_decision(parsed)
    output_reply = _converse_output_reply(result.get("output"))
    if not reply and action in ("answer", "ignore"):
        reply = output_reply

    # Resolve the conversation channel_ref for outbound reply.
    channel_ref = f"conv:{job.conversation_id}"
    if job.conversation_id:
        with conn.cursor() as cur:
            cur.execute("SELECT channel_ref FROM conversations WHERE id=%s",
                        (job.conversation_id,))
            cr = cur.fetchone()
        if cr and cr[0]:
            channel_ref = cr[0]

    idem = f"converse:{job.id}"
    _attach_converse_action_destinations(conn, job, channel_ref)

    # Manager engine failed (outage/crash, job not 'done'): fall back to the
    # deterministic rule so the inbound message is never silently dropped.
    if job.status != "done":
        _converse_fallback(conn, cfg, job, idem, channel_ref)
        return

    if action == "answer":
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (job_id, team_id, type, risk, "
                "  destination_ref, idempotency_key, payload) "
                "VALUES (%s,%s,'reply','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (job.id, job.team_id, channel_ref, idem,
                 psycopg.types.json.Json({"text": reply or "Done."})))
        status.set_status(conn, job.event_id, status.DONE)

    elif action == "dispatch":
        work = collapse_repeat(task)
        if too_vague_to_dispatch(work):
            # Too thin to build from: ask for specifics instead of opening a
            # junk PR (owner-reported "not working" -> PR, 2026-06-19).
            need_detail = (reply or
                           "I need a bit more detail to act on that, what's "
                           "broken and where?")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO actions (job_id, team_id, type, risk, "
                    "  destination_ref, idempotency_key, payload) "
                    "VALUES (%s,%s,'reply','reversible_internal',%s,%s,%s) "
                    "ON CONFLICT (idempotency_key) DO NOTHING",
                    (job.id, job.team_id, channel_ref, idem,
                     psycopg.types.json.Json({"text": need_detail})))
            status.set_status(conn, job.event_id, status.DONE)
            return
        # Update the source event payload text to the manager's task (cleaned).
        # Use to_jsonb() so the value is treated as jsonb (jsonb_set requires jsonb).
        if work and job.event_id:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE events SET payload=jsonb_set(payload, '{text}', to_jsonb(%s::text)) "
                    "WHERE id=%s",
                    (work, job.event_id))
        # Open a pipeline request for the dev team (idempotent via fingerprint).
        open_request(conn, cfg, event_id=job.event_id, team_id=job.team_id,
                     conversation_id=job.conversation_id,
                     fingerprint=idem)
        # Ack reply to the conversation.
        ack_text = reply or "On it, I'll investigate and open a PR."
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (job_id, team_id, type, risk, "
                "  destination_ref, idempotency_key, payload) "
                "VALUES (%s,%s,'reply','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (job.id, job.team_id, channel_ref, idem,
                 psycopg.types.json.Json({"text": ack_text})))

    else:
        # ignore: still acknowledge the owner. A converse job is owner-chat, so
        # silence reads as "the bot ignored me" (owner hit this: "merge it" and
        # "mark both as read" got zero reply). Send the manager's reply when it
        # gave one, else a short ack. Garbled result (no reply) -> "Got it.".
        ack = reply or "Got it."
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO actions (job_id, team_id, type, risk, "
                "  destination_ref, idempotency_key, payload) "
                "VALUES (%s,%s,'reply','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (job.id, job.team_id, channel_ref, idem,
                 psycopg.types.json.Json({"text": ack})))
        status.set_status(conn, job.event_id, status.DONE)


def _converse_output_reply(output) -> str:
    text = str(output or "").strip()
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ARGUS_RESULT") or stripped.startswith("ARGUS_ACTIONS"):
            break
        lines.append(line)
    reply = "\n".join(lines).strip()
    if len(reply) > 3000:
        return reply[:2997].rstrip() + "..."
    return reply


def _attach_converse_action_destinations(conn: psycopg.Connection, job: Job,
                                         channel_ref: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET destination_ref=%s, updated_at=now() "
            "WHERE job_id=%s AND destination_ref IS NULL "
            "AND type IN ('email_list','email_search','email_read')",
            (channel_ref, job.id),
        )


def _converse_fallback(conn: psycopg.Connection, cfg, job: Job, idem: str,
                       channel_ref: str) -> None:
    """Deterministic rule fallback when the manager engine failed. Applies the
    work-verb rule to the original message so a real task still dispatches, then
    inserts the marker action (keyed idem) so the job is handled exactly once."""
    from argus.v2.front import front
    payload = {}
    if job.event_id:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM events WHERE id=%s", (job.event_id,))
            r = cur.fetchone()
        if r and r[0]:
            payload = r[0]
    decision = front.decide(cfg, {"payload": payload})
    if decision.kind == "dispatch":
        open_request(conn, cfg, event_id=job.event_id, team_id=job.team_id,
                     conversation_id=job.conversation_id, fingerprint=idem)
        text = "On it, I'll investigate and open a PR."
    else:
        text = decision.reply_text or "Sorry, I had trouble with that. Please try again."
        status.set_status(conn, job.event_id, status.DONE)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (job_id, team_id, type, risk, destination_ref, "
            "  idempotency_key, payload) "
            "VALUES (%s,%s,'reply','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (job.id, job.team_id, channel_ref, idem,
             psycopg.types.json.Json({"text": text})))
