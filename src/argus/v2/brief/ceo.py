"""CEO brief: high-level Argus and repo health."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json

from argus.v2.pm import pending

Runner = Callable[[list[str], Path], str]


@dataclass(frozen=True)
class Brief:
    text: str
    pending_prs: int
    pending_patches: int
    failed_jobs: int
    pending_actions: int
    agent_attention: int = 0


def build(conn: psycopg.Connection, cfg, *, runner: Runner | None = None,
          health_lines: list[str] | None = None) -> Brief:
    reqs = _status_counts(conn, "requests")
    actions = _status_counts(conn, "actions")
    digests = pending.build_digests(cfg, runner=runner) if runner else pending.build_digests(cfg)
    pending_prs = sum(d.prs for d in digests)
    pending_patches = sum(d.patches for d in digests)
    failed_jobs = _failed_job_count(conn)
    pending_actions = actions.get("proposed", 0) + actions.get("awaiting_approval", 0)
    agent_lines, agent_attention = _project_agent_lines(conn, cfg)
    health = health_lines if health_lines is not None else _launchd_health()
    lines = [
        "CEO Brief",
        "",
        f"Overall: {_overall(failed_jobs, pending_actions, pending_prs, pending_patches, agent_attention)}",
        "",
        "Health:",
        *_bullets(health[:8] or ["launchd health unavailable"]),
        "",
        "Pending:",
        f"- PRs awaiting review: {pending_prs}",
        f"- Patches awaiting review: {pending_patches}",
        f"- Actions pending/approval: {pending_actions}",
        f"- Open requests: {reqs.get('open', 0)}",
        f"- Recent failed/dead agents: {failed_jobs}",
        "",
        "Agent visibility:",
        *_bullets(agent_lines),
        "",
        "Top priorities:",
        *_priorities(digests, failed_jobs, pending_actions, agent_lines),
    ]
    return Brief("\n".join(lines).strip(), pending_prs, pending_patches,
                 failed_jobs, pending_actions, agent_attention)


def notify(conn: psycopg.Connection, cfg, brief: Brief, *,
           key: str | None = None) -> bool:
    dest = _ceo_destination(cfg)
    if not dest:
        return False
    idem = key or f"ceo-brief:{datetime.utcnow().strftime('%Y-%m-%d')}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, destination_ref, "
            "idempotency_key, payload) "
            "VALUES ('ceo-brief','notify','reversible_internal',%s,%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (dest, idem, Json({"text": brief.text})),
        )
        return cur.rowcount == 1


def should_send_once(*, now: datetime | None = None,
                     timezone: str = "UTC",
                     run_root: Path | None = None) -> bool:
    now = now or datetime.now(ZoneInfo(timezone))
    local = now.astimezone(ZoneInfo(timezone))
    if local.hour != 9 or local.minute >= 30:
        return False
    marker = _marker_path(local.date().isoformat(), run_root)
    if marker.exists():
        return False
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(local.isoformat() + "\n", encoding="utf-8")
    return True


def _status_counts(conn: psycopg.Connection, table: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT status, count(*) FROM {table} GROUP BY status")
        return {str(status): int(count) for status, count in cur.fetchall()}


def _failed_job_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM jobs j
            LEFT JOIN requests r ON r.id = j.request_id
            WHERE j.status IN ('failed','dead')
              AND j.updated_at >= now() - interval '24 hours'
              AND (j.request_id IS NULL OR r.status NOT IN ('done','cancelled'))
            """
        )
        return int(cur.fetchone()[0])


def _launchd_health() -> list[str]:
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    rows = [line for line in proc.stdout.splitlines() if "com.argus" in line]
    bad = [line.strip() for line in rows if _launchd_row_needs_attention(line)]
    if bad:
        return [f"attention: {line}" for line in bad[:8]]
    return [f"{len(rows)} Argus launchd jobs loaded"]


def _launchd_row_needs_attention(line: str) -> bool:
    parts = line.split()
    if len(parts) < 3:
        return False
    pid, status = parts[0], parts[1]
    if pid != "-":
        return False
    return status != "0"


def _overall(failed_jobs: int, pending_actions: int,
             pending_prs: int, pending_patches: int,
             agent_attention: int = 0) -> str:
    if failed_jobs:
        return "needs attention"
    if pending_actions or pending_prs or pending_patches or agent_attention:
        return "pending review"
    return "healthy"


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _priorities(digests: list[pending.ProjectDigest],
                failed_jobs: int, pending_actions: int,
                agent_lines: list[str] | None = None) -> list[str]:
    out: list[str] = []
    if failed_jobs:
        out.append(f"- Inspect {failed_jobs} recent failed/dead agent(s)")
    if pending_actions:
        out.append(f"- Review {pending_actions} pending action(s)")
    for line in agent_lines or []:
        if len(out) >= 3:
            break
        if line.startswith("Action:"):
            out.append(f"- {line}")
    for digest in sorted(digests, key=lambda d: d.prs + d.patches, reverse=True):
        if len(out) >= 3:
            break
        total = digest.prs + digest.patches
        if total:
            out.append(f"- Review {digest.project}: {digest.prs} PR(s), {digest.patches} patch(es)")
    if not out:
        out.append("- No urgent action")
    return out[:3]


def _project_agent_lines(conn: psycopg.Connection, cfg) -> tuple[list[str], int]:
    projects = [team.name for team in cfg.teams if team.project is not None]
    if not projects:
        return ["no project teams configured"], 0

    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        now = cur.fetchone()[0]

    lines: list[str] = []
    quiet = 0
    quiet_prs = 0
    attention = 0
    for project in projects:
        row = _project_agent_row(conn, project)
        last_event, open_req, active_jobs, failed_jobs, pending_actions, prs7, support_ready, guidance = row
        problems: list[str] = []
        if failed_jobs:
            problems.append(f"{failed_jobs} failed/dead agent(s)")
        if open_req:
            problems.append(f"{open_req} open request(s)")
        if active_jobs:
            problems.append(f"{active_jobs} active job(s)")
        if pending_actions:
            problems.append(f"{pending_actions} pending action(s)")
        if support_ready:
            problems.append(f"{support_ready} support draft(s) ready")
        if guidance:
            problems.append(f"{guidance} support guidance pending")
        if last_event is None:
            problems.append("no telemetry yet")
        elif (now - last_event).total_seconds() > 72 * 3600:
            problems.append(f"stale, no event since {last_event:%Y-%m-%d %H:%M}")

        if problems:
            attention += 1
            lines.append(f"Action: {project}: " + ", ".join(problems))
        else:
            quiet += 1
            quiet_prs += prs7

    if quiet:
        lines.append(
            f"Quiet: {quiet} project agent(s) checked, "
            f"{quiet_prs} PR(s) opened last 7d, no action required")
    return lines[:8], attention


def _project_agent_row(conn: psycopg.Connection, project: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              (SELECT max(received_at) FROM events WHERE team_id=%s),
              (SELECT count(*) FROM requests
               WHERE team_id=%s AND status IN ('open','awaiting_approval')),
              (SELECT count(*) FROM jobs
               WHERE team_id=%s AND status IN ('pending','claimed','running','awaiting_approval')),
              (SELECT count(*) FROM jobs
               WHERE team_id=%s AND status IN ('failed','dead')
                 AND updated_at >= now() - interval '24 hours'),
              (SELECT count(*) FROM actions
               WHERE team_id=%s AND status IN ('proposed','awaiting_approval','approved')),
              (SELECT count(*) FROM actions
               WHERE team_id=%s AND type='open_pr' AND status='done'
                 AND updated_at >= now() - interval '7 days'),
              (SELECT count(*) FROM support_drafts
               WHERE project=%s AND status='ready'),
              (SELECT count(*) FROM support_guidance
               WHERE project=%s AND status='pending')
            """,
            (project, project, project, project, project, project, project, project),
        )
        return cur.fetchone()


def _ceo_destination(cfg) -> str | None:
    try:
        team = cfg.team("ceo-brief")
    except KeyError:
        return None
    for channel in team.channels:
        if channel.role == "control" and channel.type != "cli":
            return f"{channel.type}:{channel.channel_id}"
    return None


def _marker_path(day: str, run_root: Path | None = None) -> Path:
    root = run_root or Path(os.environ.get("ARGUS_RUN_ROOT", str(Path.home() / "argus-run")))
    return root / "brief" / f"ceo-{day}.sent"
