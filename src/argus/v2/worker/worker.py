"""run_once: claim one job, heartbeat while it runs, and finalize under the
fencing token. The caller drives the loop (CLI `up`)."""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from threading import Event, Thread
from urllib.parse import urlparse

from argus.v2.browser import (
    PreviewError,
    diff_touches_ui,
    discover_preview_url,
    discover_preview_url_firebase,
    run_browser_check,
)
from argus.v2.context import assemble as ctx
from argus.v2.db import pool
from argus.v2.front import front
from argus.v2.pm import memory as pm_memory
from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord
from argus.v2.roles import contracts
from argus.v2.worker import exec as job_exec
from argus.v2.worker import liveness
from argus.v2.workspace import repo as workspace

log = logging.getLogger("argus.worker")

_TEST_OUTPUT_LIMIT = 12000
_HEARTBEAT_INTERVAL = 30  # seconds; module-level so tests can shrink it


def run_once(cfg, worker_id: str, *, include_kinds=None, exclude_kinds=None) -> bool:
    conn = pool.connect()
    try:
        job = jobs.claim(conn, worker_id, include_kinds=include_kinds,
                         exclude_kinds=exclude_kinds)
        conn.commit()
        if job is None:
            return False

        heartbeat_stop = Event()
        heartbeat_thread = _start_heartbeat(job.id, job.claim_token, heartbeat_stop)
        workdir = None
        test_exit = None
        test_context = None
        try:
            team = cfg.team(job.team_id) if job.team_id else None
            project = getattr(team, "project", None) if team else None

            # Research jobs have no request_id (read-only); give them a worktree
            # keyed by the event id so the researcher can read the repo.
            wt_key = job.request_id or (str(job.event_id) if job.kind == "research" and job.event_id else None)
            if project and wt_key:
                wt = workspace.create_worktree(project, wt_key)
                workdir = wt.path
                if _is_qa_role(team, job.role) and project.test_cmd:
                    timeout = getattr(project, "test_timeout_seconds", 900)
                    try:
                        test_run = subprocess.run(
                            project.test_cmd, shell=True, cwd=workdir,
                            capture_output=True, text=True, timeout=timeout,
                        )
                        test_exit = test_run.returncode
                        test_context = _format_test_context(
                            project.test_cmd, test_exit, test_run.stdout, test_run.stderr)
                    except subprocess.TimeoutExpired:
                        test_exit = 124
                        test_context = _format_test_context(
                            project.test_cmd, test_exit, "", f"timed out after {timeout}s")

            bundle = ctx.assemble(conn, team_id=job.team_id,
                                  conversation_id=job.conversation_id,
                                  now=datetime.now(timezone.utc), cfg=cfg,
                                  query=(job.payload or {}).get("text", ""))
            context = bundle.as_prompt()
            if test_context:
                context = f"{context}\n\n{test_context}" if context else test_context
            if job.kind in ("converse", "triage"):
                # Append the live manager state block so the LLM sees current work.
                state = front.manager_state(conn, cfg, job.team_id)
                context = f"{context}\n\n{state}" if context else state
            memory_fingerprints: list[str] = []
            if project and team and _is_builder_role(team, job.role):
                memory = pm_memory.render(conn, team_id=job.team_id)
                if memory:
                    context = f"{context}\n\n{memory}" if context else memory
                    memory_fingerprints = pm_memory.fingerprints(conn, team_id=job.team_id)
            if (team and project and _is_browser_verify_role(team, job.role)
                    and getattr(project, "browser_verify", None)
                    and project.browser_verify.enabled):
                # browser-use IS the agent for this stage: no LLM role dispatch.
                run, result, actions = _run_browser_verify(job, project, workdir)
            else:
                run, result, actions = job_exec.run_job(cfg, job, context=context,
                                                        workdir=workdir)
                result["parsed"] = contracts.parse_result(run.output or "")
            if memory_fingerprints:
                result["memory_fingerprints"] = memory_fingerprints
            actions = _harden_actions(cfg, job, actions)
            if test_exit is not None:
                result["test_exit"] = test_exit
                result["test_output"] = test_context

            # Commit any edits the builder made in the worktree so the work branch
            # actually contains the change before qa runs the tests.
            if workdir and team and project:
                role_obj = None
                try:
                    role_obj = team.role(job.role)
                except KeyError:
                    pass
                if role_obj and role_obj.kind == "builder":
                    request_text = (job.payload or {}).get("text", job.role)
                    workspace.commit_all(workdir, f"{job.role}: {request_text}")
                    has_diff = bool(workspace.diff(project, workdir).strip())
                    result["has_diff"] = has_diff
                    if not has_diff:
                        parsed = dict(result.get("parsed") or {})
                        parsed.setdefault("ready", False)
                        parsed.setdefault("analysis", run.output or "No change produced.")
                        result["parsed"] = parsed

            # Deterministic liveness: surface a run that ended without making
            # progress (planned only / blocked / waiting on approval / missing
            # creds) instead of letting it look silently done.
            result["liveness"] = liveness.classify(run.output or "",
                                                    has_diff=result.get("has_diff"))
            if result["liveness"] in liveness.STUCK:
                log.info("job %s liveness=%s (no forward progress) team=%s request=%s",
                         job.id, result["liveness"], job.team_id, job.request_id,
                         extra={"job_id": job.id, "team_id": job.team_id,
                                "request_id": job.request_id})

            status = "done" if run.status == "ok" else "failed"
            jobs.finalize(conn, job.id, job.claim_token, status=status,
                          result=result, run=run, actions=actions)
            conn.commit()
            return True
        except Exception as exc:
            message = f"Worker failed before completion: {type(exc).__name__}: {exc}"
            log.warning("job %s failed: %s team=%s request=%s",
                        job.id, message, job.team_id, job.request_id, exc_info=True,
                        extra={"job_id": job.id, "team_id": job.team_id,
                               "request_id": job.request_id})
            result = {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "parsed": {"ready": False, "analysis": message},
            }
            snap = job.exec_snapshot or {}
            run = RunRecord(
                role=job.role,
                engine=str(snap.get("engine") or "unknown"),
                model=snap.get("model"),
                prompt=snap.get("prompt"),
                output=message,
                status="failed",
                prompt_hash=snap.get("prompt_hash"),
            )
            jobs.finalize(conn, job.id, job.claim_token, status="failed",
                          result=result, run=run, actions=[])
            conn.commit()
            return True
        finally:
            _stop_heartbeat(heartbeat_stop, heartbeat_thread)
    finally:
        conn.close()


def _harden_actions(cfg, job, actions):
    """Security boundary for model-emitted actions (ARGUS_ACTIONS).

    For EVERY job the risk is recomputed server-side (executor.risk_for) so the
    model can never tag merge_pr/deploy as reversible to skip the approval gate.
    Converse/manager jobs are further restricted: only the allowlisted PR ops,
    and their target repo + number are forced server-side (never trust the
    model's repo, or it could close PRs in any repo the gh token can reach)."""
    if not actions:
        return actions
    from dataclasses import replace
    from argus.v2.actions.executor import (
        risk_for, _CONVERSE_ALLOWLIST, _CONVERSE_PERSONAL_ALLOWLIST,
        _CONVERSE_TEAM_EMAIL_ALLOWLIST, _PR_NUMBER_OPS)
    repo = front._gh_owner_repo(cfg, job.team_id) if job.kind == "converse" else None
    out = []
    for a in actions:
        if job.kind == "converse":
            allowed = _CONVERSE_ALLOWLIST
            if _has_team_email_source(cfg, job.team_id):
                allowed = allowed | _CONVERSE_TEAM_EMAIL_ALLOWLIST
            if job.team_id == "personal":
                allowed = allowed | _CONVERSE_PERSONAL_ALLOWLIST
            if a.type not in allowed:
                continue  # not a manager-permitted op
            if a.type in _PR_NUMBER_OPS:
                if not repo:
                    continue  # no server repo to scope to: drop, do not trust model repo
                payload = dict(a.payload or {})
                try:
                    n = int(payload.get("number"))
                except (TypeError, ValueError):
                    continue  # invalid PR number: drop
                if n <= 0:
                    continue
                payload["number"] = n
                payload["repo"] = repo  # server-scoped; the model's repo is ignored
                a = replace(a, payload=payload)
        out.append(replace(a, risk=risk_for(a.type)))
    return out


def _has_team_email_source(cfg, team_id: str | None) -> bool:
    if cfg is None or not team_id:
        return False
    try:
        team = cfg.team(team_id)
    except KeyError:
        return False
    email_types = {"gmail_apps_script", "email_apps_script", "support_apps_script"}
    for source in list(team.sources) + list(cfg.company.sources):
        if source.type in email_types and (source.scope == "team" or source.team in (None, team_id)):
            return True
    return False


def _format_test_context(command: str, exit_code: int, stdout: str, stderr: str) -> str:
    parts = []
    if stdout:
        parts.append(stdout.rstrip())
    if stderr:
        parts.append(stderr.rstrip())
    output = "\n".join(parts).strip() or "(no output)"
    if len(output) > _TEST_OUTPUT_LIMIT:
        output = output[-_TEST_OUTPUT_LIMIT:]
    return f"TEST RESULT\ncommand: {command}\nexit_code: {exit_code}\noutput:\n{output}"


def _start_heartbeat(job_id: str, claim_token: str | None, stop: Event) -> Thread | None:
    if not claim_token:
        return None

    def beat() -> None:
        conn = pool.connect()
        while not stop.wait(_HEARTBEAT_INTERVAL):
            try:
                jobs.heartbeat(conn, job_id, claim_token)
                conn.commit()
            except Exception as exc:
                # The heartbeat connection can be poisoned by a server restart
                # or network blip. Silently giving up here would let the lease
                # expire and the job get reclaimed and re-run by another worker
                # while this one is still working it. Reconnect and keep trying
                # so renewal survives a transient outage.
                log.warning("job %s heartbeat failed, reconnecting: %s", job_id, exc)
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn = pool.connect()
                except Exception as reconnect_exc:
                    log.warning("job %s heartbeat reconnect failed: %s", job_id, reconnect_exc)
        conn.close()

    thread = Thread(target=beat, name=f"argus-job-heartbeat-{job_id}", daemon=True)
    thread.start()
    return thread


def _stop_heartbeat(stop: Event, thread: Thread | None) -> None:
    stop.set()
    if thread:
        thread.join(timeout=2)


def _is_qa_role(team, role_name: str) -> bool:
    """True if this role is the qa judge in the team's pipeline."""
    try:
        role = team.role(role_name)
        return role.kind == "judge" and role_name == "qa"
    except KeyError:
        return False


def _is_builder_role(team, role_name: str) -> bool:
    try:
        return team.role(role_name).kind == "builder"
    except KeyError:
        return False


def _is_browser_verify_role(team, role_name: str) -> bool:
    try:
        return team.role(role_name).kind == "judge" and role_name == "browser_verify"
    except KeyError:
        return False


def _bv_result(verdict: str, reason: str, *, url=None, skipped=False,
               prompt: str = "", raw: str = ""):
    """Build the (run, result, actions) triple for a browser_verify job so the
    normal finalize path stores it and the pipeline reads parsed.verdict."""
    parsed = {"verdict": verdict, "analysis": reason, "ready": verdict == "pass"}
    result = {
        "parsed": parsed,
        "browser_verify": {"verdict": verdict, "reason": reason, "url": url,
                           "skipped": skipped},
    }
    run = RunRecord(role="browser_verify", engine="browser-use", status="ok",
                    prompt=prompt or None, output=(raw or reason))
    return run, result, []


def _run_browser_verify(job, project, workdir):
    """Run the browser_verify stage: gate on the diff, push the branch, poll the
    Vercel preview, and run a browser-use agent. Returns (run, result, actions).
    Fail-closed: any error => verdict 'fail' (the PR is drafted, a human reviews)."""
    bv = project.browser_verify
    summary = (job.payload or {}).get("text", job.role)
    diff = workspace.diff(project, workdir)
    if not diff_touches_ui(diff, bv.ui_globs):
        return _bv_result("pass", "no UI files changed; browser check skipped",
                          skipped=True, prompt=summary)
    changed = [ln[len("+++ b/"):].strip() for ln in diff.splitlines()
               if ln.startswith("+++ b/") and "/dev/null" not in ln]
    branch = f"{project.work_branch_prefix}/{job.request_id}"
    try:
        if bv.discovery == "firebase":
            # Build + deploy the change's own Firebase preview channel from the
            # worktree (no branch push; the CI preview is PR-triggered).
            url = discover_preview_url_firebase(
                workdir=workdir, project=bv.firebase_project,
                channel=f"argus-{str(job.request_id)[:12]}",
                build_cmd=bv.firebase_build_cmd,
                expires=bv.firebase_channel_expires,
                build_timeout_seconds=bv.firebase_build_timeout_seconds,
            )
        else:
            token = os.environ.get(bv.vercel_token_env, "")
            workspace.push(project, branch, workdir)
            url = discover_preview_url(
                project_id=bv.vercel_project_id, branch=branch, token=token,
                team_id=bv.vercel_team_id,
                build_timeout_seconds=bv.build_timeout_seconds,
                poll_interval_seconds=bv.poll_interval_seconds,
            )
        allowed = [h for h in [urlparse(url).hostname, bv.api_host] if h]
        bv_model = bv.hermes_model if bv.backend == "hermes" else bv.browser_model
        res = run_browser_check(
            preview_url=url, base_path=bv.base_path, changed_files=changed,
            summary=summary, allowed_domains=allowed, model=bv_model,
            test_login=bv.test_login, browser_venv_python=bv.browser_venv_python,
            backend=bv.backend,
        )
        return _bv_result(res.verdict, res.reason, url=url, prompt=summary, raw=res.raw)
    except PreviewError as exc:
        return _bv_result("fail", f"preview unavailable: {exc}", prompt=summary)
    except Exception as exc:  # noqa: BLE001 - fail-closed
        return _bv_result("fail", f"browser verify error: {exc}", prompt=summary)
