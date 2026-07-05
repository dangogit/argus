"""CEO brief: high-level Argus and repo health."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json

from argus.v2.pm import pending

Runner = Callable[[list[str], Path], str]

_MAX_PR_LINES = 5


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
    prs = _pending_prs(cfg, runner)
    failed_jobs = _failed_job_count(conn)
    pending_actions = actions.get("proposed", 0) + actions.get("awaiting_approval", 0)
    guidance = _pending_guidance(conn)
    drafts = _ready_draft_counts(conn)
    health = health_lines if health_lines is not None else _launchd_health()
    bad_health = [line for line in health if line.startswith("attention:")]
    needs = _needs_you(prs, guidance, drafts, pending_actions, failed_jobs, bad_health)
    today = datetime.now(timezone.utc).date().isoformat()
    overall = "needs attention" if (failed_jobs or bad_health) else "pending review"
    if needs:
        lines = [f"CEO Brief {today}: {overall}", "", "Needs you:", *needs]
    else:
        lines = [f"CEO Brief {today}: all healthy, nothing needs you."]
    lines += ["", _fyi_line(conn, reqs)]
    dash = os.environ.get("ARGUS_DASHBOARD_URL", "").strip()
    if dash:
        lines.append(f"Dashboard: {dash}")
    return Brief("\n".join(lines).strip(), len(prs), 0, failed_jobs,
                 pending_actions, len(needs))


def notify(conn: psycopg.Connection, cfg, brief: Brief, *,
           key: str | None = None) -> bool:
    dest = _ceo_destination(cfg)
    if not dest:
        return False
    idem = key or f"ceo-brief:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
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


def _pending_prs(cfg, runner: Runner | None) -> list[pending.PendingPr]:
    run = runner or pending._run
    out: list[pending.PendingPr] = []
    for team in cfg.teams:
        if team.project is None:
            continue
        out += pending.pending_prs(team.name, Path(team.project.repo),
                                   team.project.work_branch_prefix, runner=run)
    return out


def _pending_guidance(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project, id, subject FROM support_guidance "
            "WHERE status='pending' ORDER BY created_at DESC LIMIT 5")
        return [(str(p), str(i), str(s or "")) for p, i, s in cur.fetchall()]


def _ready_draft_counts(conn: psycopg.Connection) -> list[tuple[str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project, count(*) FROM support_drafts WHERE status='ready' "
            "GROUP BY project ORDER BY project")
        return [(str(p), int(c)) for p, c in cur.fetchall()]


def _needs_you(prs: list[pending.PendingPr], guidance: list[tuple[str, str, str]],
               drafts: list[tuple[str, int]], pending_actions: int,
               failed_jobs: int, bad_health: list[str]) -> list[str]:
    out: list[str] = []
    for pr in prs[:_MAX_PR_LINES]:
        draft = " (draft)" if pr.draft else ""
        out.append(f"- [{pr.project}] PR #{pr.number}{draft}: {pr.title}")
        if pr.url:
            out.append(f"  {pr.url}")
    if len(prs) > _MAX_PR_LINES:
        out.append(f"- plus {len(prs) - _MAX_PR_LINES} more open PR(s)")
    for project, gid, subject in guidance:
        out.append(f'- [{project}] support guidance pending: "{subject}" '
                   f"(reply in its Slack thread, ID {gid})")
    for project, count in drafts:
        out.append(f"- [{project}] {count} support draft(s) ready to send")
    if pending_actions:
        out.append(f"- Approve {pending_actions} pending action(s)")
    if failed_jobs:
        out.append(f"- Inspect {failed_jobs} failed/dead agent(s) in last 24h")
    for line in bad_health:
        out.append(f"- launchd {line}")
    return out


def _fyi_line(conn: psycopg.Connection, reqs: dict[str, int]) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM jobs WHERE status IN "
            "('pending','claimed','running','awaiting_approval')")
        active = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM actions WHERE type='open_pr' AND status='done' "
            "AND updated_at >= now() - interval '7 days'")
        prs7 = int(cur.fetchone()[0])
    return (f"FYI: {active} active job(s), {reqs.get('open', 0)} open request(s), "
            f"{prs7} PR(s) opened last 7d.")


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
