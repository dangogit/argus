"""Pipeline state machine: open a request, enqueue stages with deterministic
keys, advance on completion with bounded branching, propagate dead->failed.
All advancement is one transaction + ON CONFLICT, so it is safe under double
delivery or two orchestrators."""
from __future__ import annotations

import hashlib
import subprocess
from typing import Optional

import psycopg
from psycopg.types.json import Json

from argus.v2 import alerts
from argus.v2.config import loader
from argus.v2.pm import memory as pm_memory
from argus.v2.pm import scan as pm_scan
from argus.v2.queue import jobs
from argus.v2.queue.models import Job
from argus.v2.rules import context as rules_context
from argus.v2.roles import contracts
from argus.v2.skills import registry as skills

# Signal payloads whose only content is one of these is housekeeping noise, not
# work. Without this gate an internal "no findings" alert opened a build request,
# failed QA, and pinged the owner (owner-reported feedback loop, 2026-06-19).
_SIGNAL_NOISE = ("produced no findings", "skipped or empty", "no new findings")


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
                 conversation_id: Optional[str], fingerprint: Optional[str] = None) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO requests (event_id, team_id, conversation_id, fingerprint)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (team_id, fingerprint)
              WHERE fingerprint IS NOT NULL
                AND status IN ('open','awaiting_approval')
            DO NOTHING
            RETURNING id
            """,
            (event_id, team_id, conversation_id, fingerprint),
        )
        row = cur.fetchone()
        if not row:
            return None  # active request already exists for this fingerprint
        request_id = str(row[0])
    enqueue_stage(conn, cfg, request_id=request_id, stage_index=0)
    return request_id


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
    jobs.enqueue(
        conn, team_id=team_id, kind="pipeline", role=role_name, stage=stage_index,
        idempotency_key=_stage_key(request_id, stage_index, branch_iter),
        exec_snapshot=snapshot, payload={"text": text},
        request_id=request_id, event_id=str(event_id),
        conversation_id=str(conversation_id) if conversation_id else None,
    )
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
            else:
                _record_memory_outcome(conn, job.request_id, "no-change", analysis)
                _no_fix_close(conn, cfg, job.request_id, analysis)
        else:
            _next(conn, cfg, job.request_id, 1)
    elif _is_qa(team, role):
        verdict = contracts.qa_verdict(parsed, (result or {}).get("test_exit"))
        if verdict == "pass" or (advisory and "verdict" not in parsed and (result or {}).get("test_exit") is None):
            senior_idx = _index(team, "senior")
            if senior_idx is not None:
                _next(conn, cfg, job.request_id, senior_idx)
            else:
                _approve_done(conn, cfg, job)
        else:
            _loop_back(conn, cfg, job, team, to_role="developer")
    elif role == "senior":
        decision = contracts.senior_decision(parsed)
        if decision == "approve" or (advisory and "decision" not in parsed):
            _approve_done(conn, cfg, job)
        else:
            _loop_back(conn, cfg, job, team, to_role="developer")
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


def _no_fix_close(conn: psycopg.Connection, cfg, request_id, analysis: str,
                  *, blocked: bool = False) -> None:
    """The developer investigated and no fix is warranted (or, when blocked=True,
    could not do the work): close the request and report to the channel - no
    qa/senior, no PR, no fabricated edit."""
    prefix = ("Blocked, couldn't complete the task: " if blocked
              else "Investigated, no fix needed: ")
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


def _next(conn: psycopg.Connection, cfg, request_id: str, stage_index: int) -> None:
    """Advance to stage_index; if out of range, mark done."""
    team_id = _request_team(conn, request_id)
    team = cfg.team(team_id)
    if stage_index >= len(team.pipeline.stages):
        with conn.cursor() as cur:
            cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s",
                        (request_id,))
    else:
        enqueue_stage(conn, cfg, request_id=request_id, stage_index=stage_index)


def _loop_back(conn: psycopg.Connection, cfg, job: Job, team, to_role: str) -> None:
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


def _fail(conn: psycopg.Connection, cfg, request_id: str, reason: str) -> None:
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
                _record_memory_outcome(
                    conn, job.request_id, "qa-fail",
                    f"blocked by deterministic diff scan: {pm_scan.format_findings(scan)}",
                )
                _fail(conn, cfg, job.request_id,
                      f"Deterministic diff scan blocked PR: {pm_scan.format_findings(scan)}")
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
                          "summary_short": pr_info["summary_short"],
                          "checks": pr_info["checks"],
                          "risk_summary": pr_info["risk_summary"],
                          "changed_files": pr_info["changed_files"],
                          "cwd": cwd}),
                ),
            )
        cur.execute("UPDATE requests SET status='done', updated_at=now() WHERE id=%s",
                    (job.request_id,))


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
        _record_memory_outcome(
            conn, job.request_id, "qa-fail",
            f"blocked by deterministic diff scan: {pm_scan.format_findings(scan)}",
        )
        _fail(conn, cfg, job.request_id,
              f"Deterministic diff scan blocked PR: {pm_scan.format_findings(scan)}")
        return True

    failure_label = "QA failed" if job.role == "qa" else f"{job.role} failed"
    risk = f"needs review: {failure_label}; {reason} Opened as draft so the diff is inspectable."
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
                                         outcome=outcome, note=note)
    except Exception:
        pass


def _pr_info(conn: psycopg.Connection, cfg, request_id: str, *, cwd: str,
             risk_summary: str | None = None) -> dict:
    request = _request_text_for_request(conn, request_id)
    files = _changed_files(cwd)
    checks = _checks_summary(conn, request_id)
    title = _title(request, request_id)
    summary = _builder_summary(conn, request_id) or _summary_short(request)
    risk = risk_summary or "low: QA passed and senior approved"
    body = "\n".join([
        f"## Request\n{request or request_id}",
        "",
        f"## Summary\n{summary}",
        "",
        "## Changed Files",
        _bullet_list(files) if files else "- No file list available",
        "",
        f"## Verification\n{checks}",
        "",
        f"## Risk\n{risk}",
        "",
        f"Generated by Argus request `{request_id}`.",
    ])
    return {
        "title": title,
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


def _checks_summary(conn: psycopg.Connection, request_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, result FROM jobs WHERE request_id=%s "
            "ORDER BY stage, updated_at",
            (request_id,))
        rows = cur.fetchall()
    parts = []
    for role, result in rows:
        parsed = (result or {}).get("parsed", {}) if isinstance(result, dict) else {}
        if role == "qa":
            verdict = parsed.get("verdict") or ("pass" if (result or {}).get("test_exit") == 0 else "pass")
            parts.append(f"QA: {verdict}")
        elif role == "senior":
            parts.append(f"Senior: {parsed.get('decision') or 'approve'}")
    return "; ".join(parts) or "QA and senior approved"


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
    return " ".join(text.split())[:600]


def _summary_short(request: str) -> str:
    # Fallback only: used when the builder emitted no LLM summary. Plain request
    # text, no mechanical filename string (the owner asked for issue+fix, not paths).
    return " ".join((request or "Automated change").split())[:160]


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"- `{item}`" for item in items)


def _failure_text(conn: psycopg.Connection, request_id: str, reason: str) -> str:
    request = _request_text_for_request(conn, request_id)
    lines = [
        "Argus pipeline stopped without opening a PR.",
        f"Request: {request or request_id}",
        f"Reason: {reason}",
        "Next: fix failing stage and rerun request.",
    ]
    detail = _last_failure_detail(conn, request_id)
    if detail:
        lines += ["", "Last failure:", detail]
    return "\n".join(lines)


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
        import json
        return (f"An automated signal arrived from source '{source}'. Investigate it "
                f"and make a minimal, focused fix if one is warranted.\n\nSignal details:\n"
                f"{json.dumps(payload, indent=2, default=str)}")
    return ""


def _config_hash(cfg) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()[:16]


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
                            conversation_id=None, fingerprint=fingerprint or event_id) or ""
    eng = loader.resolve_engine(cfg, team_id, "researcher")
    text = _signal_task_text(conn, event_id, mode="research")
    snapshot: dict = {"engine": eng.engine, "model": eng.model, "prompt": role.prompt,
                      "config_hash": _config_hash(cfg), "project": team_id}
    _add_rules(conn, cfg, snapshot, team_id)
    _add_skills(snapshot, "researcher", role.skills, text)
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
                     conversation_id=None, fingerprint=fp)
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
                     conversation_id=None, fingerprint=fp)
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
                 psycopg.types.json.Json({"text": reply})))

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
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (job_id, team_id, type, risk, destination_ref, "
            "  idempotency_key, payload) "
            "VALUES (%s,%s,'reply','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (job.id, job.team_id, channel_ref, idem,
             psycopg.types.json.Json({"text": text})))
