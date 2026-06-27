"""Pending PM review digest and cleanup for v2 launchd jobs."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.types.json import Json

from argus.v2.evals import judge as eval_judge
from argus.v2.evals import rubric as eval_rubric
from argus.v2.evals.judge import EvalResult


Runner = Callable[[list[str], Path], str]


@dataclass(frozen=True)
class PendingPr:
    project: str
    number: int
    title: str
    url: str
    draft: bool
    created: str
    body: str = ""


@dataclass(frozen=True)
class PendingPatch:
    project: str
    fingerprint: str
    patch: str
    senior: str
    qa: str


@dataclass(frozen=True)
class ProjectDigest:
    project: str
    text: str
    prs: int
    patches: int


@dataclass(frozen=True)
class DraftCleanResult:
    project: str
    number: int
    action: str
    url: str


def build_digests(cfg, projects: list[str] | None = None,
                  *, run_root: Path | None = None,
                  runner: Runner | None = None) -> list[ProjectDigest]:
    runner = runner or _run
    selected = projects or [team.name for team in cfg.teams if team.project is not None]
    digests: list[ProjectDigest] = []
    for project in selected:
        team = cfg.team(project)
        if team.project is None:
            continue
        prs = pending_prs(project, Path(team.project.repo),
                          team.project.work_branch_prefix, runner=runner)
        patches = pending_patches(project, run_root)
        text = render_digest(project, prs, patches)
        if text:
            digests.append(ProjectDigest(project=project, text=text,
                                         prs=len(prs), patches=len(patches)))
    return digests


def pending_prs(project: str, repo: Path, branch_prefix: str,
                *, runner: Runner) -> list[PendingPr]:
    if not repo.exists():
        return []
    try:
        raw = runner([
            "gh", "pr", "list", "--state", "open", "--limit", "100",
            "--json", "number,title,url,isDraft,headRefName,createdAt,body",
        ], repo)
    except Exception:
        return []
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    prefix = branch_prefix.rstrip("/") + "/"
    out: list[PendingPr] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        head = str(row.get("headRefName") or "")
        if not (head == branch_prefix or head.startswith(prefix)):
            continue
        out.append(PendingPr(
            project=project,
            number=int(row.get("number") or 0),
            title=str(row.get("title") or ""),
            url=str(row.get("url") or ""),
            draft=bool(row.get("isDraft")),
            created=str(row.get("createdAt") or ""),
            body=str(row.get("body") or ""),
        ))
    return out


def pending_patches(project: str, run_root: Path) -> list[PendingPatch]:
    return []


def render_digest(project: str, prs: list[PendingPr],
                  patches: list[PendingPatch]) -> str:
    if not prs and not patches:
        return ""
    lines = [
        f"PM work awaiting your review: {len(prs)} open PR(s), "
        f"{len(patches)} patch(es)",
    ]
    if prs:
        lines += ["", "Open PRs:"]
        for pr in prs:
            draft = " (draft)" if pr.draft else ""
            lines += [f"- [{project}] PR #{pr.number}{draft}: {pr.title}", f"  {pr.url}"]
            about = _about_pr(pr)
            if about:
                lines.append(f"  {about}")
    if patches:
        lines += ["", "Patches (apply or reject, then delete the result json):"]
        for patch in patches:
            lines += [
                f"- [{project}] patch {patch.fingerprint} "
                f"(senior:{patch.senior} qa:{patch.qa})",
                f"  {patch.patch}",
            ]
    return "\n".join(lines)


def notify_digests(conn: psycopg.Connection, cfg, digests: list[ProjectDigest]) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    with conn.cursor() as cur:
        for digest in digests:
            dest = _control_destination(cfg, digest.project)
            if not dest:
                continue
            cur.execute(
                "INSERT INTO actions (team_id, type, risk, destination_ref, "
                "idempotency_key, payload) "
                "VALUES (%s,'notify','reversible_internal',%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (
                    digest.project,
                    dest,
                    f"pm-pending:{digest.project}:{today}",
                    Json({"text": digest.text}),
                ),
            )
            count += cur.rowcount
    return count


def clean_drafts(cfg, projects: list[str] | None = None,
                 *, runner: Runner | None = None,
                 judge: Callable[[str, str], EvalResult] | None = None,
                 ) -> list[DraftCleanResult]:
    runner = runner or _run
    selected = projects or [team.name for team in cfg.teams if team.project is not None]
    results: list[DraftCleanResult] = []
    for project in selected:
        team = cfg.team(project)
        if team.project is None:
            continue
        repo = Path(team.project.repo)
        threshold = team.project.pm.eval_threshold
        prs = pending_prs(project, repo, team.project.work_branch_prefix, runner=runner)
        for pr in prs:
            if not pr.draft:
                continue
            if not _checks_pass(repo, pr.number, runner):
                body = "Closing old Argus draft: checks are not passing or unavailable. Rerun Argus to open a ready PR."
                runner(["gh", "pr", "comment", str(pr.number), "--body", body], repo)
                runner(["gh", "pr", "close", str(pr.number)], repo)
                results.append(DraftCleanResult(project, pr.number, "closed", pr.url))
                continue
            # CI passed; the eval gate (when enabled) is the second, model-based grader.
            held = _eval_held(cfg, project, repo, pr, runner, judge, threshold)
            if held is not None:
                body = (f"Argus eval gate held this PR (score {held.score:.2f} < "
                        f"{threshold:.2f}). {held.notes}")
                runner(["gh", "pr", "comment", str(pr.number), "--body", body], repo)
                results.append(DraftCleanResult(project, pr.number, "held", pr.url))
                continue
            runner(["gh", "pr", "ready", str(pr.number)], repo)
            results.append(DraftCleanResult(project, pr.number, "ready", pr.url))
    return results


def _eval_held(cfg, project: str, repo: Path, pr: PendingPr, runner: Runner,
               judge: Callable[[str, str], EvalResult] | None,
               threshold: float | None) -> EvalResult | None:
    """Run the eval gate. Returns the EvalResult when the gate is ENABLED and the
    PR FAILS it (caller holds the draft); returns None when the gate is disabled
    or the PR passes (caller promotes). A judge/diff error fails safe -> held."""
    if threshold is None:
        return None
    try:
        diff = runner(["gh", "pr", "diff", str(pr.number)], repo)
    except Exception:
        return EvalResult(score=0.0, notes="eval: could not read PR diff (held)")
    j = judge or _make_judge(cfg, project)
    try:
        result = j(diff, eval_rubric.load(repo))
    except Exception as e:  # never let a judge failure auto-promote
        return EvalResult(score=0.0, notes=f"eval: judge error (held): {e}")
    return None if result.score >= threshold else result


def _make_judge(cfg, project: str) -> Callable[[str, str], EvalResult]:
    """Default production judge: a stateless model call on an independent engine
    (env ARGUS_EVAL_ENGINE, else the project's judge engine, else company default)."""
    engine = os.environ.get("ARGUS_EVAL_ENGINE")
    if not engine:
        try:
            from argus.v2.config import loader as _loader
            engine = _loader.resolve_engine(cfg, project, "senior").engine
        except Exception:
            default = cfg.company.defaults.engine
            engine = default.engine if default else "echo"

    def _run_engine(prompt: str) -> str:
        from argus.engine import run_agent
        return run_agent(engine, prompt).text

    return lambda diff, rubric: eval_judge.score_diff(diff, rubric, run=_run_engine)


def _control_destination(cfg, project: str) -> str | None:
    team = cfg.team(project)
    for channel in team.channels:
        if channel.role == "control" and channel.type != "cli":
            return f"{channel.type}:{channel.channel_id}"
    return None


def _about_pr(pr: PendingPr) -> str:
    body = " ".join((pr.body or "").split())
    for marker in ("## Summary", "Summary"):
        if marker in pr.body:
            after = pr.body.split(marker, 1)[1].strip()
            line = next((ln.strip("# ").strip() for ln in after.splitlines() if ln.strip()), "")
            if line:
                return line[:180]
    return body[:180]


def _checks_pass(repo: Path, number: int, runner: Runner) -> bool:
    try:
        out = runner([
            "gh", "pr", "checks", str(number),
            "--json", "state",
            "--jq", 'all(.[]; .state == "SUCCESS" or .state == "SKIPPED")',
        ], repo)
    except Exception:
        return False
    return out.strip().lower() == "true"


def _run(argv: list[str], cwd: Path) -> str:  # pragma: no cover
    proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"{argv[0]} failed")
    return proc.stdout
