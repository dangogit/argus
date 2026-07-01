"""Argus CLI. Run as `python -m argus.v2.cli <cmd>`. ARGUS_CONFIG points at
argus.yaml; ARGUS_DB_DSN at Postgres."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from argus.v2 import alerts
from argus.v2 import envfile
from argus.v2.actions import approvals
from argus.v2.config import loader
from argus.v2.db import migrate, pool
from argus.v2.ingress import events
from argus.v2.orchestrator import loop, reconcile
from argus.v2.queue import jobs as queue
from argus.v2.worker import worker

WOW_FIRST_TASK = (
    "Make one tiny safe improvement in this repo and open a draft PR. "
    "Prefer docs or tests if code risk is unclear."
)


def _cfg():
    path = os.environ.get("ARGUS_CONFIG") or os.environ["ARGUS_CONFIG_V2"]
    return loader.load(Path(path))


def _cfg_path() -> Path | None:
    path = os.environ.get("ARGUS_CONFIG") or os.environ.get("ARGUS_CONFIG_V2")
    return Path(path).expanduser() if path else None


def cmd_version(args) -> int:
    print("argus 0.2.0")
    return 0


def cmd_init(args) -> int:
    path = Path(args.config).expanduser()
    if path.exists() and not args.force:
        print(f"init: {path} already exists", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = str(Path.cwd())
    defaults = "    engine: { engine: echo }\n"
    channels = ""
    if args.channel == "telegram":
        defaults += "    webhook_secret: \"${env:ARGUS_WEBHOOK_SECRET}\"\n"
        channels = (
            "    channels:\n"
            "      - type: telegram\n"
            "        role: control\n"
            "        channel_id: \"12345\"\n"
            "        secret_ref: \"${env:TELEGRAM_BOT_TOKEN}\"\n"
        )
    elif args.channel == "slack":
        defaults += "    webhook_secret: \"${env:ARGUS_WEBHOOK_SECRET}\"\n"
        channels = (
            "    channels:\n"
            "      - type: slack\n"
            "        role: control\n"
            "        channel_id: \"C1234567890\"\n"
            "        secret_ref: \"${env:SLACK_BOT_TOKEN}\"\n"
            "        config:\n"
            "          signing_secret: \"${env:SLACK_SIGNING_SECRET}\"\n"
        )
    elif args.channel == "whatsapp":
        defaults += "    webhook_secret: \"${env:ARGUS_WEBHOOK_SECRET}\"\n"
        channels = (
            "    channels:\n"
            "      - type: whatsapp\n"
            "        role: control\n"
            "        channel_id: \"120363_REPLACE_ME@g.us\"\n"
            "        secret_ref: \"${env:ARGUS_WA_APIKEY}\"\n"
            "        config:\n"
            "          base_url: \"${env:ARGUS_WA_URL}\"\n"
            "          instance: \"${env:ARGUS_WA_INSTANCE}\"\n"
        )
    path.write_text(
        "company:\n"
        "  name: argus\n"
        "  defaults:\n"
        f"{defaults}"
        "teams:\n"
        "  - name: demo\n"
        f"    project: {{ repo: {repo}, base_branch: main }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        f"{channels}",
        encoding="utf-8",
    )
    print(path)
    return 0


def cmd_onboard(args) -> int:
    from argus.v2 import onboarding

    if args.onboard_cmd != "project":
        return 1
    config = Path(args.config).expanduser() if args.config else (_cfg_path() or Path("argus.yaml"))
    written = onboarding.write_project_onboarding(
        Path(args.path),
        config_path=config,
        out_dir=Path(args.out_dir),
        mode=args.mode,
        channel=args.channel,
        channel_id=args.channel_id,
        team_name=args.team,
        manager_engine=args.manager_engine,
        force=args.force,
    )
    for name in ("config", "env", "checklist"):
        print(f"onboard: {name}={written[name]}")
    return 0


def cmd_wow(args) -> int:
    from argus.v2 import onboarding

    project = Path(args.path).expanduser().resolve()
    if not project.exists() or not project.is_dir():
        print(f"wow: project path not found: {project}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir).expanduser()
    config = Path(args.config).expanduser() if args.config else out_dir / "argus.yaml"
    scan = onboarding.scan_project(project)
    team = args.team or scan.slug
    written = onboarding.write_project_onboarding(
        project,
        config_path=config,
        out_dir=out_dir,
        mode="pm-propose-pr",
        channel=args.channel,
        channel_id=args.channel_id,
        team_name=team,
        manager_engine=args.manager_engine,
        force=args.force,
    )
    print("ARGUS WOW")
    print(f"repo: {project}")
    print(f"team: {team}")
    print("mode: pm-propose-pr")
    print(f"channel: {args.channel}")
    print(f"config: {written['config']}")
    print(f"env: {written['env']}")
    print(f"checklist: {written['checklist']}")
    print("agent can code: true")
    print("draft PR path: developer -> qa -> senior")
    print(
        "detected: "
        f"docs={len(scan.docs)} package_files={len(scan.package_files)} "
        f"ci={len(scan.ci_workflows)} providers={','.join(scan.provider_hints) or 'none'}"
    )
    if args.no_first_task:
        print("first task: skipped by --no-first-task")
    else:
        event_id, reason = _queue_wow_first_task(
            config_path=written["config"],
            team=team,
            text=args.first_task,
        )
        if event_id:
            print(f"first task: queued PM request {event_id}")
        else:
            print(f"first task: not queued ({reason})")
    quoted_config = shlex.quote(str(written["config"]))
    quoted_task = shlex.quote(args.first_task)
    print("next:")
    print(f"  export ARGUS_CONFIG={quoted_config}")
    print("  argus db migrate")
    print("  argus doctor --deep --live --json")
    print(f"  argus submit --team {shlex.quote(team)} {quoted_task}")
    print("  argus up --iterations 3")
    return 0


def _queue_wow_first_task(*, config_path: Path, team: str, text: str) -> tuple[str | None, str]:
    if not os.environ.get("ARGUS_DB_DSN"):
        return None, "set ARGUS_DB_DSN and run argus db migrate first"
    try:
        from argus.v2.pm import autofix

        cfg = loader.load(config_path)
        conn = pool.connect()
        try:
            result = autofix.dispatch(
                conn,
                cfg,
                project=team,
                fingerprint=f"wow:first-task:{team}",
                message=text,
                source="wow",
                force=True,
            )
            conn.commit()
            if result.request_id:
                return result.request_id, ""
            return None, result.status
        finally:
            conn.close()
    except Exception as exc:
        return None, str(exc)


def cmd_doctor(args) -> int:
    if getattr(args, "deep", False):
        from argus.v2 import opscheck

        checks, rc = opscheck.doctor_deep(
            _cfg_path(),
            live=getattr(args, "live", False),
            require_source_types=set(getattr(args, "require_source_type", None) or []),
            require_team_source_types=set(getattr(args, "require_team_source_type", None) or []),
            require_each_team_source_types=set(
                getattr(args, "require_each_team_source_type", None) or []),
        )
        if getattr(args, "json", False):
            print(opscheck.report_json(checks))
        else:
            opscheck.print_checks(checks)
        return rc
    ok = True
    try:
        cfg = _cfg()
        print(f"[ok] config teams={len(cfg.teams)}")
    except Exception as exc:
        print(f"[fail] config {exc}")
        ok = False
    try:
        conn = pool.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM schema_migrations")
                print(f"[ok] db migrations={cur.fetchone()[0]}")
        finally:
            conn.close()
    except Exception as exc:
        print(f"[fail] db {exc}")
        ok = False
    if getattr(args, "live", False):
        try:
            from argus.engine import run_agent
            out = run_agent("echo", "ping").text.strip()
            print(f"[ok] engine {out[:40]}")
        except Exception as exc:
            print(f"[fail] engine {exc}")
            ok = False
    return 0 if ok else 1


def cmd_ready(args) -> int:
    rc = cmd_doctor(args)
    if rc != 0:
        return rc
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["launchctl", "list"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            count = sum(1 for line in out.splitlines() if "com.argus." in line)
            print(f"[ok] launchd argus_jobs={count}")
        except Exception as exc:
            print(f"[warn] launchd {exc}")
    return 0


def cmd_go_live(args) -> int:
    from argus.v2 import opscheck

    checks, status, rc = opscheck.go_live(
        _cfg_path(),
        mode=args.mode,
        public_url=args.public_url,
        serve_url=args.serve_url,
        dev_tunnel=args.dev_tunnel,
        skip_pr_smoke=args.skip_pr_smoke,
        skip_connectors=set(args.skip_connector or []),
        prove_slack_channels=args.prove_slack_channels,
        fresh_slack_proof=args.fresh_slack_proof,
        require_source_types=set(args.require_source_type or []),
        require_team_source_types=set(args.require_team_source_type or []),
        require_each_team_source_types=set(args.require_each_team_source_type or []),
    )
    if args.json:
        print(opscheck.report_json(checks, status=status))
    else:
        opscheck.print_checks(checks, status=status)
    return rc


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent / "db" / "migrations"


def cmd_verify(args) -> int:
    root = _source_root()
    gate = root / "scripts" / "gate.py"
    if not gate.exists():
        print("verify: source checkout required (missing scripts/gate.py)", file=sys.stderr)
        return 2
    proc = subprocess.run([sys.executable, "scripts/gate.py"], cwd=root)
    return int(proc.returncode)


def cmd_db(args) -> int:
    if args.db_cmd != "migrate":
        return 1
    migrations_dir = _migrations_dir()
    if not migrations_dir.exists() or not list(migrations_dir.glob("*.sql")):
        print(f"db migrate: migrations not found at {migrations_dir}", file=sys.stderr)
        return 2
    conn = pool.connect()
    try:
        applied = migrate.apply(conn, migrations_dir)
    finally:
        conn.close()
    print(f"db migrate: applied={len(applied)}")
    return 0


def cmd_validate(args) -> int:
    cfg = _cfg()
    names = [team.name for team in cfg.teams]
    if len(names) != len(set(names)):
        print("validate: duplicate team names", file=sys.stderr)
        return 1
    print(f"validate: ok teams={len(names)}")
    return 0


def cmd_validate_roles(args) -> int:
    cfg = _cfg()
    ok = True
    for team in cfg.teams:
        role_names = {role.name for role in team.roles}
        for stage in team.pipeline.stages:
            if stage not in role_names:
                print(f"validate-roles: {team.name} stage {stage} has no role", file=sys.stderr)
                ok = False
        for role in team.roles:
            if not role.prompt:
                print(f"validate-roles: {team.name}/{role.name} missing prompt", file=sys.stderr)
                ok = False
    print("validate-roles: ok" if ok else "validate-roles: failed")
    return 0 if ok else 1


def cmd_projects(args) -> int:
    cfg = _cfg()
    if args.projects_cmd == "list":
        for team in cfg.teams:
            print(team.name)
        return 0
    return 1


def cmd_config(args) -> int:
    if args.config_cmd == "convert":
        from argus.v2.config import convert

        project_dirs = [Path(item).expanduser() for item in args.projects_dir]
        data = convert.convert_file(Path(args.input).expanduser(), project_dirs=project_dirs)
        text = convert.dumps(data)
        if args.out:
            out = Path(args.out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            loader.load(out)
            print(out)
        else:
            print(text, end="")
        return 0
    cfg = _cfg().model_dump()
    value = cfg
    for part in args.key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            print(f"config: missing {args.key}", file=sys.stderr)
            return 1
    if isinstance(value, (dict, list)):
        print(json.dumps(value, sort_keys=True))
    else:
        print(value)
    return 0


def cmd_submit(args) -> int:
    cfg = _cfg()
    conn = pool.connect()
    try:
        dedup = args.dedup or f"cli:{int(time.time()*1000)}"
        media = []
        if args.image:
            media.append({"kind": "image", "src": args.image, "mime": "image/png"})
        if args.voice:
            media.append({"kind": "audio", "src": args.voice, "mime": "audio/ogg"})
        eid = events.ingest_message(conn, cfg, team=args.team, source="cli",
                                    dedup_key=dedup, text=args.text, media=media)
        conn.commit()
        print(f"event {eid}")
        return 0
    finally:
        conn.close()


def cmd_signal(args) -> int:
    cfg = _cfg()
    conn = pool.connect()
    try:
        payload = json.loads(args.payload) if args.payload else {}
        eid = events.ingest_signal(conn, cfg, team=args.team, source=args.source,
                                   fingerprint=args.fingerprint, payload=payload)
        conn.commit()
        print(f"signal event {eid}")
        return 0
    finally:
        conn.close()


def cmd_up(args) -> int:
    cfg = _cfg()
    # Orchestrator loop. By default a single-process dev driver: sweep + drain all
    # jobs. In production pass --sweep-only so the orchestrator ONLY sweeps (route
    # events, advance pipelines, drain actions, reclaim) and never executes a job -
    # then a slow/hung job in a worker lane can't freeze routing or chat. The
    # `argus worker --lane {chat,pipeline}` processes run the jobs concurrently.
    iterations = args.iterations
    i = 0
    while iterations is None or i < iterations:
        conn = pool.connect()
        try:
            reconcile.sweep_once(conn, cfg)
            conn.commit()
        finally:
            conn.close()
        if not getattr(args, "sweep_only", False):
            drained = True
            while drained:
                drained = worker.run_once(cfg, "w1")
        i += 1
        if iterations is None:
            time.sleep(args.poll)
    return 0


# Lane -> the kinds a worker claims. The chat lane takes owner-facing jobs; the
# pipeline lane takes everything else (so a new job kind is never stranded).
_WORKER_LANES = {
    "chat": {"worker_id": "chat", "include_kinds": list(queue.CHAT_KINDS)},
    "pipeline": {"worker_id": "pipe", "exclude_kinds": list(queue.CHAT_KINDS)},
    "all": {"worker_id": "w1"},
}


def cmd_worker(args) -> int:
    """Standalone job worker for one lane (production runs chat + pipeline lanes
    as separate processes alongside `up --sweep-only`). Drains its lane, then
    sleeps; never sweeps."""
    cfg = _cfg()
    lane = _WORKER_LANES[args.lane]
    worker_id = lane["worker_id"]
    claim_kw = {k: v for k, v in lane.items() if k != "worker_id"}
    iterations = args.iterations
    i = 0
    while iterations is None or i < iterations:
        drained = True
        while drained:
            drained = worker.run_once(cfg, worker_id, **claim_kw)
        i += 1
        if iterations is None:
            time.sleep(args.poll)
    return 0


def cmd_mcp_serve(args) -> int:
    from argus.v2.mcp import server
    return server.serve()


def cmd_status(args) -> int:
    conn = pool.connect()
    try:
        with conn.cursor() as cur:
            for table in ("events", "requests", "jobs", "actions", "approvals"):
                cur.execute(f"SELECT status, count(*) FROM {table} GROUP BY status")
                rows = cur.fetchall()
                summary = ", ".join(f"{s}={n}" for s, n in rows) or "none"
                print(f"{table}: {summary}")
        return 0
    finally:
        conn.close()


def cmd_runs(args) -> int:
    conn = pool.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.role, r.engine, r.status, left(coalesce(r.output,''),60) "
                "FROM runs r JOIN jobs j ON j.id=r.job_id WHERE j.request_id=%s "
                "ORDER BY r.started_at", (args.request_id,))
            for role, engine, status, out in cur.fetchall():
                print(f"{role:10} {engine:12} {status:6} {out}")
        return 0
    finally:
        conn.close()


def cmd_actions(args) -> int:
    conn = pool.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT type, risk, status FROM actions WHERE request_id=%s",
                        (args.request_id,))
            for t, risk, status in cur.fetchall():
                print(f"{t:10} {risk:22} {status}")
        return 0
    finally:
        conn.close()


def cmd_approve(args) -> int:
    conn = pool.connect()
    try:
        ok = approvals.consume(conn, args.nonce, decision="approved", approver_ref="cli")
        conn.commit()
        print("approved" if ok else "no pending approval for that nonce")
        return 0 if ok else 1
    finally:
        conn.close()


def cmd_reject(args) -> int:
    conn = pool.connect()
    try:
        ok = approvals.consume(conn, args.nonce, decision="rejected", approver_ref="cli")
        conn.commit()
        print("rejected" if ok else "no pending approval for that nonce")
        return 0 if ok else 1
    finally:
        conn.close()


def cmd_recover(args) -> int:
    cfg = _cfg()
    conn = pool.connect()
    try:
        n = queue.reclaim_expired(conn)
        approvals.expire_due(conn)
        conn.commit()
        print(f"reclaimed {n} jobs")
        return 0
    finally:
        conn.close()


def cmd_dead_job(args) -> int:
    conn = pool.connect()
    try:
        if args.dead_job_cmd == "list":
            for row in queue.list_dead(conn, limit=args.limit):
                died_at = row["died_at"].isoformat() if row["died_at"] else ""
                snippet = row["last_error"][:80]
                print(f"{row['id']}\t{row['team_id']}\t{row['kind']}\t"
                      f"{row['attempts']}/{row['max_attempts']}\t{snippet}\t{died_at}")
            return 0
        if args.dead_job_cmd == "retry":
            ok = queue.retry_dead(conn, args.job_id)
            conn.commit()
            print("queued for retry" if ok else "no dead job with that id")
            return 0 if ok else 1
    finally:
        conn.close()
    return 1


def cmd_poll(args) -> int:
    from argus.v2.connectors import driver
    cfg = _cfg(); conn = pool.connect()
    try:
        if args.dry_run:
            previews = driver.dry_run(
                conn,
                cfg,
                source_names=set(args.source) if args.source else None,
                source_types=set(args.source_type) if args.source_type else None,
            )
            failed = False
            for preview in previews:
                if preview.ok:
                    keys = ",".join(preview.cursor_keys) or "-"
                    print(
                        f"{preview.source}\t{preview.team}\t"
                        f"{preview.source_type}\tok\t{preview.count}\t{keys}"
                    )
                else:
                    failed = True
                    print(
                        f"{preview.source}\t{preview.team}\t"
                        f"{preview.source_type}\terror\t{preview.error_type}"
                    )
            return 1 if failed else 0
        n = driver.poll_once(
            conn,
            cfg,
            source_names=set(args.source) if args.source else None,
            source_types=set(args.source_type) if args.source_type else None,
        )
        print(f"ingested {n} new signal(s)")
        return 0
    finally:
        conn.close()


def cmd_summarize(args) -> int:
    from datetime import date
    from argus.v2.context import summarize
    cfg = _cfg(); conn = pool.connect()
    try:
        s = summarize.summarize_day(conn, cfg, team_id=args.team,
                                    conversation_id=None, day=date.fromisoformat(args.day))
        conn.commit(); print(s or "(no activity)"); return 0
    finally:
        conn.close()


def cmd_support(args) -> int:
    from argus.v2.support import state as support_state

    if args.support_cmd == "list":
        rows = support_state.draft_list(
            args.team,
            status=None if args.status == "all" else args.status,
            limit=args.limit,
        )
        for row in rows:
            print(
                f"{row['id']}\t{row['project']}\t{row['status']}\t"
                f"{row['thread_id']}\t{row['subject']}"
            )
        return 0

    if args.support_cmd == "clear":
        draft = support_state.draft_get(args.team, args.draft_id)
        if not draft:
            print(f"support clear: unknown draft {args.draft_id}", file=sys.stderr)
            return 1
        support_state.draft_set_status(args.team, args.draft_id, "cleared")
        support_state.record(
            args.team,
            draft["thread_id"],
            "archived",
            draft["sender"],
            draft["subject"],
            "draft cleared by cli",
        )
        print(f"support {args.team}: cleared {args.draft_id}")
        return 0

    if args.support_cmd == "reply":
        from argus.v2.support import cycle as support_cycle

        draft = support_state.draft_get(args.team, args.draft_id)
        if not draft:
            print(f"support reply: unknown draft {args.draft_id}", file=sys.stderr)
            return 1
        if args.dry_run:
            print(draft["reply"])
            return 0
        cfg = _cfg()
        try:
            source = support_cycle._support_source(cfg, args.team)
            support_cycle._send_customer_reply(source, source.config, draft, draft["reply"])
            support_state.draft_set_status(args.team, args.draft_id, "sent")
            support_state.record(
                args.team,
                draft["thread_id"],
                "replied",
                draft["sender"],
                draft["subject"],
                "sent by cli",
            )
        except Exception as exc:
            support_state.draft_set_status(args.team, args.draft_id, "failed")
            print(f"support reply: failed: {exc}", file=sys.stderr)
            return 1
        print(f"support {args.team}: sent {args.draft_id}")
        return 0

    from argus.v2.actions import executor
    from argus.v2.support import cycle as support_cycle

    cfg = _cfg()
    conn = pool.connect()
    try:
        result = support_cycle.run_project(conn, cfg, args.team)
        executor.process_proposed(conn, cfg)
        conn.commit()
        print(
            f"support {args.team}: proposed={result.proposed} "
            f"sent={result.sent} "
            f"archived={result.archived} escalated={result.escalated} "
            f"skipped={result.skipped}"
        )
        return 0
    finally:
        conn.close()


def cmd_pm(args) -> int:
    from argus.v2.actions import executor
    from argus.v2.pm import autofix
    from argus.v2.pm import pending

    cfg = _cfg()
    if args.pm_cmd == "run":
        conn = pool.connect()
        try:
            finding = json.loads(args.payload) if args.payload else None
            result = autofix.dispatch(
                conn, cfg, project=args.team, fingerprint=args.fingerprint,
                finding=finding, message=args.message, force=args.force,
            )
            conn.commit()
            if result.request_id:
                print(f"pm run: queued {result.request_id}")
            else:
                print(f"pm run: {result.status} {args.fingerprint}")
            return 0
        except Exception as exc:
            conn.rollback()
            print(f"pm run: failed: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
    if args.pm_cmd == "cycle":
        conn = pool.connect()
        try:
            results = autofix.cycle(conn, cfg, project=args.team)
            conn.commit()
            queued = sum(1 for r in results if r.request_id)
            print(f"pm cycle: queued={queued} scanned={len(results)}")
            return 0
        except Exception as exc:
            conn.rollback()
            print(f"pm cycle: failed: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
    if args.pm_cmd == "clean-drafts":
        results = pending.clean_drafts(cfg, args.team or None)
        if not results:
            print("pm-clean-drafts: no Argus draft PRs found")
            return 0
        for result in results:
            print(f"{result.project} PR #{result.number}: {result.action} {result.url}")
        return 0
    conn = pool.connect()
    try:
        digests = pending.build_digests(cfg, args.team or None)
        for digest in digests:
            print(f"== {digest.project}")
            print(digest.text)
        if args.notify:
            inserted = pending.notify_digests(conn, cfg, digests)
            if inserted:
                executor.process_proposed(conn, cfg)
            conn.commit()
            print(f"notified {inserted} digest(s)")
        elif not digests:
            print("pm-pending: nothing awaiting review")
        return 0
    finally:
        conn.close()


def cmd_retro(args) -> int:
    from datetime import date

    from argus.v2 import retro

    cfg = _cfg()
    conn = pool.connect()
    try:
        if args.retro_cmd == "run":
            retro_day = date.fromisoformat(args.date) if args.date else None
            synthesized, bridged = retro.run(
                conn, cfg, retro_day=retro_day, team_id=args.team,
                company_only=args.company_only,
            )
            notified = 0
            if not args.no_notify:
                notified = retro.notify(
                    conn, cfg, retro_day=retro_day, team_id=args.team,
                    company_only=args.company_only,
                )
            conn.commit()
            print(
                f"retro run: synthesized={synthesized} bridged={bridged} "
                f"notified={notified}"
            )
            return 0
        if args.retro_cmd == "notify":
            retro_day = date.fromisoformat(args.date) if args.date else None
            notified = retro.notify(
                conn, cfg, retro_day=retro_day, team_id=args.team,
                company_only=args.company_only,
            )
            conn.commit()
            print(f"retro notify: notified={notified}")
            return 0
        if args.retro_cmd == "backlog":
            for item in retro.backlog(conn, status=args.status, team_id=args.team):
                print(f"{item.team_id}\t{item.status}\t{item.type}\tprio={item.priority}\t{item.statement}")
            return 0
        if args.retro_cmd == "summary":
            print(retro.summary(conn))
            return 0
    finally:
        conn.close()
    return 1


def cmd_alert(args) -> int:
    conn = pool.connect()
    try:
        if args.alert_cmd == "add":
            payload = json.loads(args.payload) if args.payload else {}
            alert_id = alerts.record(
                conn,
                severity=args.severity,
                project=args.project,
                fingerprint=args.fingerprint,
                message=args.message,
                channel=args.channel,
                payload=payload,
            )
            conn.commit()
            print(f"alert {alert_id}")
            return 0
        if args.alert_cmd == "list":
            for alert in alerts.list_alerts(
                conn,
                limit=args.limit,
                severity=args.severity,
                project=args.project,
            ):
                print(
                    f"{alert.ts.isoformat()}\t{alert.severity}\t{alert.channel}\t"
                    f"{alert.project}\t{alert.fingerprint}\t{alert.message}"
                )
            return 0
    finally:
        conn.close()
    return 1


def cmd_state(args) -> int:
    from argus.v2 import state_import

    conn = pool.connect()
    try:
        counts = state_import.run(
            conn,
            run_root=Path(args.run_root).expanduser(),
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            conn.commit()
        parts = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        prefix = "state import dry-run" if args.dry_run else "state import"
        print(f"{prefix}: {parts}")
        return 0
    finally:
        conn.close()


def cmd_assistant(args) -> int:
    from argus.v2.assistant import memory

    if args.assistant_cmd == "memory":
        if args.memory_cmd == "refresh":
            changed = memory.refresh()
            print("memory refresh complete" if changed else "memory refresh no-op")
            return 0
        if args.memory_cmd == "show":
            text = memory.show()
            if text:
                print(text, end="" if text.endswith("\n") else "\n")
            return 0
    return 1


def cmd_advisor(args) -> int:
    if args.advisor_cmd == "ingest":
        from argus.v2.advisor import state

        row = {
            "id": args.message_id,
            "ts": args.ts or int(time.time()),
            "participant": args.participant,
            "participant_jid": args.participant_jid or args.participant,
            "push_name": args.push_name or args.participant,
            "body": args.body,
            "mentioned": args.mentioned or [],
            "quoted_participant": args.quoted_participant or "",
        }
        message_id = state.record_message(args.group, row)
        print(f"advisor message {message_id}")
        return 0
    if args.advisor_cmd == "status":
        from argus.v2.advisor import state

        for row in state.status_rows():
            print(
                f"{row['jid']}\tcursor={row['cursor']}\t"
                f"messages={row['messages']}\treplies={row['replies']}\t"
                f"digests={row['digests']}"
            )
        return 0
    if args.advisor_cmd == "tick":
        from argus.v2.advisor import tick
        processed = tick.run()
        print(f"advisor tick processed {processed}")
        return 0
    if args.advisor_cmd == "digest":
        from argus.v2.advisor import digest
        recorded = digest.run()
        print(f"advisor digest recorded {recorded}")
        return 0
    return 1


def cmd_context(args) -> int:
    if args.context_cmd == "status":
        conn = pool.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job, source, last_id
                    FROM context_watermarks
                    ORDER BY job
                    """
                )
                watermarks = cur.fetchall()
                cur.execute(
                    """
                    SELECT status, count(*)
                    FROM context_commitments
                    GROUP BY status
                    ORDER BY status
                    """
                )
                commitments = cur.fetchall()
                cur.execute(
                    """
                    SELECT status, count(*)
                    FROM conversation_contexts
                    GROUP BY status
                    ORDER BY status
                    """
                )
                contexts = cur.fetchall()
            for job, source, last_id in watermarks:
                print(f"watermark\t{job}\t{source}\t{last_id}")
            for status, count in commitments:
                print(f"commitments\t{status}\t{count}")
            for status, count in contexts:
                print(f"contexts\t{status}\t{count}")
            return 0
        finally:
            conn.close()
    if args.context_cmd == "commitments":
        from argus.v2.context import state

        for row in state.commit_list(args.status):
            print(
                f"{row['id']}\t{row['status']}\t{row['due_at']}\t"
                f"{row['who']}\t{row['what']}"
            )
        return 0
    if args.context_cmd in {"done", "dismiss"}:
        from argus.v2.context import state

        status = "done" if args.context_cmd == "done" else "dismissed"
        try:
            state.commit_set(args.commitment_id, "status", status)
        except ValueError as exc:
            print(f"context {args.context_cmd}: {exc}", file=sys.stderr)
            return 1
        print(f"context {args.context_cmd}: {args.commitment_id}")
        return 0
    if args.context_cmd == "snooze":
        from argus.v2.context import state

        try:
            state.commit_set(args.commitment_id, "snooze_until", args.until)
            state.commit_set(args.commitment_id, "status", "open")
        except ValueError as exc:
            print(f"context snooze: {exc}", file=sys.stderr)
            return 1
        print(f"context snooze: {args.commitment_id} until {args.until}")
        return 0
    if args.context_cmd == "recall":
        conn = pool.connect()
        try:
            like = f"%{args.query}%"
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, status, who, what, due_at
                    FROM context_commitments
                    WHERE what ILIKE %s OR who ILIKE %s OR source_ref ILIKE %s
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (like, like, like, args.limit),
                )
                commitments = cur.fetchall()
                cur.execute(
                    """
                    SELECT context_ref, status, context_type, summary
                    FROM conversation_contexts
                    WHERE summary ILIKE %s OR payload::text ILIKE %s
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (like, like, args.limit),
                )
                contexts = cur.fetchall()
            for cid, status, who, what, due_at in commitments:
                print(f"commitment\t{cid}\t{status}\t{due_at}\t{who}\t{what}")
            for ref, status, ctype, summary in contexts:
                print(f"context\t{ref}\t{status}\t{ctype}\t{summary}")
            return 0
        finally:
            conn.close()
    if args.context_cmd == "distill":
        from argus.v2.context import distill
        try:
            result = distill.run()
        except Exception as exc:
            print(f"context distill: failed: {exc}", file=sys.stderr)
            return 1
        if result.no_new:
            print("context distill: no new rows")
            return 0
        if result.outage:
            print("context distill: engine outage", file=sys.stderr)
            return 1
        print(f"context distill: done (watermark {result.watermark})")
        return 0
    if args.context_cmd == "remind":
        from argus.v2.context import remind
        result = remind.run()
        if result.failed:
            print("context remind: failed", file=sys.stderr)
            return 1
        if result.no_due:
            print("context remind: no reminders due")
            return 0
        print(f"context remind: delivered {result.delivered} reminder(s)")
        return 0
    return 1


def cmd_content(args) -> int:
    if args.content_cmd == "draft":
        from argus.v2.content import drain, state

        queue_id = state.queue_add(args.project, args.platform, args.request)
        if not args.drain:
            print(f"content queue {queue_id}")
            return 0
        result = drain.run()
        if result.failed:
            print("content draft: drain failed", file=sys.stderr)
            return 1
        if result.draft_id:
            print(f"content draft {result.draft_id}")
        else:
            print(f"content queue {queue_id}")
        return 0
    if args.content_cmd == "list":
        from argus.v2.content import state

        draft_status = None if args.status == "all" else args.status
        for row in state.draft_list(draft_status, limit=args.limit):
            print(
                f"draft\t{row['id']}\t{row['project']}\t"
                f"{row['platform']}\t{row['status']}"
            )
        queue_status = None if args.queue_status == "all" else args.queue_status
        for row in state.queue_list(queue_status, limit=args.limit):
            print(
                f"queue\t{row['id']}\t{row['project']}\t"
                f"{row['platform']}\t{row['status']}\t{row['request']}"
            )
        return 0
    if args.content_cmd == "publish":
        from argus.v2.actions import handlers
        from argus.v2.content import state

        draft = state.latest_draft(args.draft_id)
        if not draft:
            print(f"content publish: unknown draft {args.draft_id}", file=sys.stderr)
            return 1
        payload = {
            "draft_id": draft["id"],
            "project": draft["project"],
            "platform": draft["platform"],
            "path": str(state.content_dir() / draft["id"]),
        }
        try:
            ref = handlers.run("social_publish", payload)
            state.set_draft_status(args.draft_id, "published")
        except Exception as exc:
            state.set_draft_status(args.draft_id, "failed")
            print(f"content publish: failed: {exc}", file=sys.stderr)
            return 1
        print(f"content publish: {ref or args.draft_id}")
        return 0
    if args.content_cmd == "drain":
        from argus.v2.content import drain
        result = drain.run()
        if result.no_queue:
            print("content drain: no queued briefs")
            return 0
        if result.failed:
            print("content drain: failed", file=sys.stderr)
            return 1
        print(f"content drain: draft {result.draft_id} ready")
        return 0
    return 1


def cmd_brief(args) -> int:
    from argus.v2.actions import executor
    from argus.v2.brief import ceo

    cfg = _cfg()
    conn = pool.connect()
    try:
        if args.brief_cmd == "ceo":
            if args.once_per_day and not ceo.should_send_once(timezone=args.timezone):
                print("ceo brief: not due")
                return 0
            brief = ceo.build(conn, cfg)
            if args.notify:
                inserted = ceo.notify(conn, cfg, brief)
                if inserted:
                    executor.process_proposed(conn, cfg)
                conn.commit()
                print(f"ceo brief: notified={int(inserted)}")
            else:
                print(brief.text)
            return 0
        return 1
    finally:
        conn.close()


def cmd_history(args) -> int:
    conn = pool.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT day, summary FROM conversation_summaries WHERE team_id=%s "
                        "ORDER BY day DESC LIMIT %s", (args.team, args.days))
            for day, s in cur.fetchall():
                print(f"{day}: {s}")
        return 0
    finally:
        conn.close()


def cmd_know(args) -> int:
    from argus.v2.knowledge import store
    cfg = _cfg(); conn = pool.connect()
    try:
        if args.know_cmd == "add":
            kid = store.add(conn, cfg, scope=args.scope, team_id=args.team,
                            title=args.title, content=args.content, source="cli")
            conn.commit(); print(f"knowledge {kid}")
        else:
            for r in store.search(conn, cfg, team_id=args.team, query=args.query, k=args.k):
                print(f"- {r['title']}: {r['content']}")
        return 0
    finally:
        conn.close()


def cmd_serve(args) -> int:  # pragma: no cover
    from argus.v2.channels import receiver
    cfg = _cfg()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not receiver.has_inbound_auth(cfg):
        print("argus serve: refusing non-loopback host without inbound auth", file=sys.stderr)
        return 2
    receiver.serve(cfg, host=args.host, port=args.port)
    return 0


def cmd_inbound(args) -> int:
    from argus.v2.channels import receiver

    cfg = _cfg()
    if args.file:
        body = Path(args.file).expanduser().read_bytes()
    elif args.payload:
        body = args.payload.encode("utf-8")
    else:
        body = sys.stdin.buffer.read()
    headers = {}
    secret = args.secret or getattr(cfg.company.defaults, "webhook_secret", None)
    if secret:
        headers["x-argus-secret"] = secret
    conn = pool.connect()
    try:
        status, count = receiver.handle_webhook(conn, cfg, args.channel, body, headers)
        print(f"{args.label} handle: status={status} ingested={count}")
        return 0 if status == 200 else 1
    finally:
        conn.close()


def cmd_host(args) -> int:
    from argus.v2 import host

    if args.host_cmd in {"render", "install"}:
        dirs = [Path(item).expanduser() for item in args.jobs_dir]
        if not dirs and os.environ.get("ARGUS_HOST_JOBS_DIR"):
            dirs = [
                Path(item).expanduser()
                for item in os.environ["ARGUS_HOST_JOBS_DIR"].split(os.pathsep)
                if item
            ]
        jobs = host.discover_jobs(dirs)
        if args.host_cmd == "render":
            paths = host.write_jobs(jobs, Path(args.out), os_name=args.os)
        else:
            paths = host.install(jobs, dry_run=args.dry_run)
        for path in paths:
            print(path)
        return 0
    if args.host_cmd == "uninstall":
        for path in host.uninstall(dry_run=args.dry_run):
            print(path)
        return 0
    if args.host_cmd == "status":
        print(host.status())
        return 0
    if args.host_cmd == "watchdog":
        from argus.v2 import system_health
        from argus.v2.actions import executor

        rc = cmd_ready(argparse.Namespace(live=args.live))
        cfg_path = _cfg_path()
        notified = False
        try:
            try:
                cfg = _cfg()
            except Exception:
                if cfg_path is None:
                    raise
                cfg = system_health.load_general_routing_config(cfg_path)
            conn = pool.connect()
            try:
                findings = system_health.collect_findings(
                    config_path=cfg_path,
                    readiness_failed=rc != 0,
                )
                inserted = system_health.notify_findings(conn, cfg, findings)
                if inserted:
                    executor.process_proposed(conn, cfg)
                    notified = True
                # Remediate after notifying so the alert captures the live PID,
                # and gated by conn so each restart is cooldown-throttled + audited.
                remediated = system_health.remediate_findings(findings, conn=conn)
                if remediated:
                    print("watchdog: restarted " + ", ".join(remediated))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            if rc != 0:
                try:
                    # Stable fingerprint + cooldown so a sustained outage alerts
                    # at most once an hour instead of every 15-minute run.
                    alerts.emit(
                        severity="error",
                        project="general",
                        fingerprint="argus-watchdog-readiness",
                        message="Argus watchdog: readiness failed",
                        channel="whatsapp",
                        cooldown_seconds=3600,
                    )
                    notified = True
                except Exception:
                    pass
        if notified:
            print("watchdog: health notification queued")
        return rc
    if args.host_cmd == "backup":
        run_root = Path(args.run_root or os.environ.get("ARGUS_RUN_ROOT", "run")).expanduser()
        dest_root = Path(args.dest or os.environ.get("ARGUS_BACKUP_DIR", str(Path.home() / "argus-backups"))).expanduser()
        dest = host.backup(run_root, dest_root, db_dsn=os.environ.get("ARGUS_DB_DSN", ""))
        print(dest)
        return 0
    if args.host_cmd == "logrotate":
        log_dir = Path(args.log_dir or os.environ.get("ARGUS_LOG_DIR", str(Path.home() / "Library" / "Logs" / "argus"))).expanduser()
        rotated = host.logrotate(log_dir, max_bytes=args.max_bytes, retain_days=args.retain)
        print(f"logrotate: rotated={rotated}")
        return 0
    return 1


def cmd_calendar(args) -> int:
    from argus.v2 import calendar

    params = {
        "id": getattr(args, "id", None),
        "title": getattr(args, "title", None),
        "start": getattr(args, "start", None),
        "end": getattr(args, "end", None),
        "duration_min": getattr(args, "duration_min", None),
        "description": getattr(args, "description", None),
        "location": getattr(args, "location", None),
        "guests": getattr(args, "guests", None),
        "tz": getattr(args, "tz", None),
        "days": getattr(args, "days", None),
        "from": getattr(args, "from_", None),
        "to": getattr(args, "to", None),
        "all_day": getattr(args, "all_day", False),
    }
    params = {key: value for key, value in params.items() if value not in (None, "", False)}
    try:
        print(calendar.run(args.calendar_cmd, params, json_output=args.json))
        return 0
    except calendar.CalendarError as exc:
        print(f"calendar: {exc}", file=sys.stderr)
        return exc.code


def cmd_launchd(args) -> int:
    from argus.v2 import launchd
    env_files = list(args.env_file or [])
    env_files.extend(envfile.split_env_files(args.env_files))
    config = args.config or os.environ.get("ARGUS_CONFIG") or os.environ["ARGUS_CONFIG_V2"]
    db_dsn = args.db_dsn or os.environ.get("ARGUS_DB_DSN", "")
    if not db_dsn and not env_files:
        print("argus launchd render: provide --db-dsn, ARGUS_DB_DSN, or --env-file", file=sys.stderr)
        return 2
    units = launchd.default_units(
        python=args.python,
        config=config,
        db_dsn=db_dsn,
        run_root=args.run_root or os.environ.get("ARGUS_RUN_ROOT", str(Path.home() / "argus-run")),
        env_files=env_files,
        log_dir=args.log_dir,
        label_prefix=args.label_prefix,
    )
    # `launchd render` defaults to macOS plists (its historical behavior);
    # Linux systemd units are opt-in via --os linux.
    os_name = args.os or "macos"
    for path in launchd.write_units(units, Path(args.out), os_name=os_name):
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="argus")
    p.add_argument("--version", action="version", version="argus 0.2.0")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("version"); s.set_defaults(fn=cmd_version)
    s = sub.add_parser("init")
    s.add_argument("--config", default="argus.yaml")
    s.add_argument("--force", action="store_true")
    s.add_argument("--channel", choices=["none", "telegram", "slack", "whatsapp"],
                   default="none")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("wow")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--config")
    s.add_argument("--out-dir", default="argus-wow")
    s.add_argument("--team")
    s.add_argument("--channel", choices=["none", "cli", "fake", "telegram", "slack", "whatsapp"],
                   default="cli")
    s.add_argument("--channel-id")
    s.add_argument("--manager-engine",
                   choices=["echo", "scripted", "codex", "claude-code", "hermes"],
                   default="codex")
    s.add_argument("--first-task", default=WOW_FIRST_TASK)
    s.add_argument("--no-first-task", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_wow)
    s = sub.add_parser("onboard")
    obs = s.add_subparsers(dest="onboard_cmd", required=True)
    r = obs.add_parser("project")
    r.add_argument("path")
    r.add_argument("--mode", choices=["chat-only", "monitor-only", "pm-propose-pr"],
                   default="chat-only")
    r.add_argument("--config")
    r.add_argument("--out-dir", default=".")
    r.add_argument("--team")
    r.add_argument("--channel", choices=["none", "cli", "fake", "telegram", "slack", "whatsapp"],
                   default="cli")
    r.add_argument("--channel-id")
    r.add_argument("--manager-engine",
                   choices=["echo", "scripted", "codex", "claude-code", "hermes"],
                   default="codex")
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_onboard)
    s = sub.add_parser("doctor")
    s.add_argument("--live", action="store_true")
    s.add_argument("--deep", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--require-source-type", action="append", default=[])
    s.add_argument("--require-team-source-type", action="append", default=[])
    s.add_argument("--require-each-team-source-type", action="append", default=[])
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("ready")
    s.add_argument("--live", action="store_true")
    s.set_defaults(fn=cmd_ready)
    s = sub.add_parser("go-live")
    s.add_argument("--mode", choices=["chat-only", "monitor-only", "pm-propose-pr"],
                   default="chat-only")
    s.add_argument("--public-url")
    s.add_argument("--serve-url", default="http://127.0.0.1:8787")
    s.add_argument("--dev-tunnel", action="store_true")
    s.add_argument("--skip-pr-smoke", action="store_true")
    s.add_argument("--skip-connector", action="append", default=[])
    s.add_argument("--prove-slack-channels", action="store_true")
    s.add_argument("--fresh-slack-proof", action="store_true")
    s.add_argument("--require-source-type", action="append", default=[])
    s.add_argument("--require-team-source-type", action="append", default=[])
    s.add_argument("--require-each-team-source-type", action="append", default=[])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_go_live)
    s = sub.add_parser("verify"); s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("validate"); s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("validate-roles"); s.set_defaults(fn=cmd_validate_roles)
    s = sub.add_parser("mcp")
    mcps = s.add_subparsers(dest="mcp_cmd", required=True)
    mcps.add_parser("serve").set_defaults(fn=cmd_mcp_serve)
    s = sub.add_parser("db")
    dbs = s.add_subparsers(dest="db_cmd", required=True)
    r = dbs.add_parser("migrate"); r.set_defaults(fn=cmd_db)
    s = sub.add_parser("projects")
    prs = s.add_subparsers(dest="projects_cmd", required=True)
    r = prs.add_parser("list"); r.set_defaults(fn=cmd_projects)
    s = sub.add_parser("config")
    cfs = s.add_subparsers(dest="config_cmd", required=True)
    r = cfs.add_parser("get"); r.add_argument("key"); r.set_defaults(fn=cmd_config)
    r = cfs.add_parser("convert")
    r.add_argument("--input", required=True)
    r.add_argument("--out")
    r.add_argument("--projects-dir", action="append", default=[])
    r.set_defaults(fn=cmd_config)

    s = sub.add_parser("submit"); s.add_argument("--team", required=True)
    s.add_argument("text"); s.add_argument("--dedup"); s.add_argument("--image")
    s.add_argument("--voice"); s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("signal"); s.add_argument("--team", required=True)
    s.add_argument("--source", required=True); s.add_argument("--fingerprint", required=True)
    s.add_argument("payload", nargs="?", default="{}"); s.set_defaults(fn=cmd_signal)

    s = sub.add_parser("up"); s.add_argument("--poll", type=float, default=2.0)
    s.add_argument("--iterations", type=int, default=None)
    s.add_argument("--sweep-only", action="store_true",
                   help="only sweep (route/advance/drain); run jobs via `worker` lanes")
    s.set_defaults(fn=cmd_up)

    s = sub.add_parser("worker")
    s.add_argument("--lane", choices=["chat", "pipeline", "all"], default="all")
    s.add_argument("--poll", type=float, default=2.0)
    s.add_argument("--iterations", type=int, default=None)
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("runs"); s.add_argument("request_id"); s.set_defaults(fn=cmd_runs)
    s = sub.add_parser("actions"); s.add_argument("request_id"); s.set_defaults(fn=cmd_actions)
    s = sub.add_parser("approve"); s.add_argument("nonce"); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("reject"); s.add_argument("nonce"); s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("recover"); s.set_defaults(fn=cmd_recover)
    s = sub.add_parser("dead-job")
    djs = s.add_subparsers(dest="dead_job_cmd", required=True)
    r = djs.add_parser("list")
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(fn=cmd_dead_job)
    r = djs.add_parser("retry")
    r.add_argument("job_id")
    r.set_defaults(fn=cmd_dead_job)
    s = sub.add_parser("poll")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--source", action="append", default=[])
    s.add_argument("--source-type", action="append", default=[])
    s.set_defaults(fn=cmd_poll)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787); s.set_defaults(fn=cmd_serve)

    def add_inbound_handle(parent, *, command_name: str, label: str):
        parsers = parent.add_subparsers(dest=f"{command_name}_cmd", required=True)
        handle = parsers.add_parser("handle")
        handle.add_argument("--channel", default="whatsapp")
        handle.add_argument("--file")
        handle.add_argument("--secret")
        handle.add_argument("payload", nargs="?")
        handle.set_defaults(fn=cmd_inbound, label=label)

    s = sub.add_parser("inbound")
    add_inbound_handle(s, command_name="inbound", label="inbound")

    s = sub.add_parser("wa")
    add_inbound_handle(s, command_name="wa", label="wa")

    s = sub.add_parser("host")
    hs = s.add_subparsers(dest="host_cmd", required=True)
    r = hs.add_parser("render")
    r.add_argument("--out", required=True)
    r.add_argument("--os", choices=["macos", "linux"], default=None)
    r.add_argument("--jobs-dir", action="append", default=[])
    r.set_defaults(fn=cmd_host)
    r = hs.add_parser("install")
    r.add_argument("--jobs-dir", action="append", default=[])
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_host)
    r = hs.add_parser("uninstall")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_host)
    r = hs.add_parser("status"); r.set_defaults(fn=cmd_host)
    r = hs.add_parser("watchdog")
    r.add_argument("--live", action="store_true")
    r.set_defaults(fn=cmd_host)
    r = hs.add_parser("backup")
    r.add_argument("--run-root")
    r.add_argument("--dest")
    r.set_defaults(fn=cmd_host)
    r = hs.add_parser("logrotate")
    r.add_argument("--log-dir")
    r.add_argument("--max-bytes", type=int, default=int(os.environ.get("ARGUS_LOGROTATE_MAX_BYTES", "52428800")))
    r.add_argument("--retain", type=int, default=int(os.environ.get("ARGUS_LOGROTATE_RETAIN", "7")))
    r.set_defaults(fn=cmd_host)

    s = sub.add_parser("calendar")
    cas = s.add_subparsers(dest="calendar_cmd", required=True)
    r = cas.add_parser("ping")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_calendar)
    r = cas.add_parser("list")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--from", dest="from_")
    r.add_argument("--to")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_calendar)
    r = cas.add_parser("get")
    r.add_argument("--id", required=True)
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_calendar)
    r = cas.add_parser("create")
    r.add_argument("--title", required=True)
    r.add_argument("--start", required=True)
    r.add_argument("--end")
    r.add_argument("--duration", dest="duration_min", type=int)
    r.add_argument("--desc", "--description", dest="description")
    r.add_argument("--location")
    r.add_argument("--guests")
    r.add_argument("--tz")
    r.add_argument("--all-day", action="store_true")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_calendar)
    r = cas.add_parser("update")
    r.add_argument("--id", required=True)
    r.add_argument("--title")
    r.add_argument("--start")
    r.add_argument("--end")
    r.add_argument("--duration", dest="duration_min", type=int)
    r.add_argument("--desc", "--description", dest="description")
    r.add_argument("--location")
    r.add_argument("--guests")
    r.add_argument("--tz")
    r.add_argument("--all-day", action="store_true")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_calendar)
    r = cas.add_parser("delete")
    r.add_argument("--id", required=True)
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_calendar)

    s = sub.add_parser("launchd")
    ls = s.add_subparsers(dest="launchd_cmd", required=True)
    r = ls.add_parser("render")
    r.add_argument("--out", required=True)
    r.add_argument("--python", default=sys.executable)
    r.add_argument("--config")
    r.add_argument("--db-dsn")
    r.add_argument("--run-root")
    r.add_argument("--env-file", action="append", default=[])
    r.add_argument("--env-files", default="")
    r.add_argument("--log-dir", default=str(Path.home() / "Library" / "Logs" / "argus"))
    r.add_argument("--label-prefix", default="com.argus")
    r.add_argument("--os", choices=["macos", "linux"], default=None,
                   help="target init system (default: detected host)")
    r.set_defaults(fn=cmd_launchd)

    s = sub.add_parser("summarize"); s.add_argument("--team", required=True)
    s.add_argument("--day", required=True); s.set_defaults(fn=cmd_summarize)
    s = sub.add_parser("support")
    ss = s.add_subparsers(dest="support_cmd", required=True)
    r = ss.add_parser("run"); r.add_argument("--team", required=True)
    r.set_defaults(fn=cmd_support)
    r = ss.add_parser("list")
    r.add_argument("--team")
    r.add_argument("--status", default="ready", choices=["ready", "sent", "cleared", "failed", "all"])
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(fn=cmd_support)
    r = ss.add_parser("reply")
    r.add_argument("--team", required=True)
    r.add_argument("draft_id")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_support)
    r = ss.add_parser("clear")
    r.add_argument("--team", required=True)
    r.add_argument("draft_id")
    r.set_defaults(fn=cmd_support)
    s = sub.add_parser("pm")
    ps = s.add_subparsers(dest="pm_cmd", required=True)
    r = ps.add_parser("pending")
    r.add_argument("--notify", action="store_true")
    r.add_argument("team", nargs="*")
    r.set_defaults(fn=cmd_pm)
    r = ps.add_parser("run")
    r.add_argument("team")
    r.add_argument("fingerprint")
    r.add_argument("--message")
    r.add_argument("--payload")
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_pm)
    r = ps.add_parser("cycle")
    r.add_argument("team")
    r.set_defaults(fn=cmd_pm)
    r = ps.add_parser("clean-drafts")
    r.add_argument("team", nargs="*")
    r.set_defaults(fn=cmd_pm)
    s = sub.add_parser("retro")
    rs = s.add_subparsers(dest="retro_cmd", required=True)
    r = rs.add_parser("run")
    r.add_argument("--date")
    r.add_argument("--team")
    r.add_argument("--company-only", action="store_true")
    r.add_argument("--no-notify", action="store_true")
    r.set_defaults(fn=cmd_retro)
    r = rs.add_parser("notify")
    r.add_argument("--date")
    r.add_argument("--team")
    r.add_argument("--company-only", action="store_true")
    r.set_defaults(fn=cmd_retro)
    r = rs.add_parser("backlog")
    r.add_argument("--status")
    r.add_argument("--team")
    r.set_defaults(fn=cmd_retro)
    r = rs.add_parser("summary")
    r.set_defaults(fn=cmd_retro)
    s = sub.add_parser("alert")
    als = s.add_subparsers(dest="alert_cmd", required=True)
    r = als.add_parser("add")
    r.add_argument("--severity", required=True, choices=sorted(alerts.SEVERITIES))
    r.add_argument("--project", required=True)
    r.add_argument("--fingerprint", required=True)
    r.add_argument("--message", required=True)
    r.add_argument("--channel", choices=sorted(alerts.CHANNELS))
    r.add_argument("--payload")
    r.set_defaults(fn=cmd_alert)
    r = als.add_parser("list")
    r.add_argument("--limit", type=int, default=50)
    r.add_argument("--severity", choices=sorted(alerts.SEVERITIES))
    r.add_argument("--project")
    r.set_defaults(fn=cmd_alert)
    s = sub.add_parser("state")
    sts = s.add_subparsers(dest="state_cmd", required=True)
    r = sts.add_parser("import")
    r.add_argument("--run-root", default=os.environ.get("ARGUS_RUN_ROOT", "~/argus-run"))
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_state)
    s = sub.add_parser("assistant")
    aps = s.add_subparsers(dest="assistant_cmd", required=True)
    m = aps.add_parser("memory")
    ms = m.add_subparsers(dest="memory_cmd", required=True)
    r = ms.add_parser("refresh"); r.set_defaults(fn=cmd_assistant)
    r = ms.add_parser("show"); r.set_defaults(fn=cmd_assistant)
    s = sub.add_parser("advisor")
    ads = s.add_subparsers(dest="advisor_cmd", required=True)
    r = ads.add_parser("ingest")
    r.add_argument("--group", required=True)
    r.add_argument("--message-id", required=True)
    r.add_argument("--participant", required=True)
    r.add_argument("--participant-jid")
    r.add_argument("--push-name")
    r.add_argument("--mentioned", action="append", default=[])
    r.add_argument("--quoted-participant")
    r.add_argument("--ts", type=int)
    r.add_argument("body")
    r.set_defaults(fn=cmd_advisor)
    r = ads.add_parser("status"); r.set_defaults(fn=cmd_advisor)
    r = ads.add_parser("tick"); r.set_defaults(fn=cmd_advisor)
    r = ads.add_parser("digest"); r.set_defaults(fn=cmd_advisor)
    s = sub.add_parser("context")
    cs = s.add_subparsers(dest="context_cmd", required=True)
    r = cs.add_parser("status"); r.set_defaults(fn=cmd_context)
    r = cs.add_parser("commitments")
    r.add_argument("--status", default="open", choices=["open", "surfaced", "done", "dismissed"])
    r.set_defaults(fn=cmd_context)
    r = cs.add_parser("done"); r.add_argument("commitment_id"); r.set_defaults(fn=cmd_context)
    r = cs.add_parser("snooze")
    r.add_argument("commitment_id")
    r.add_argument("--until", required=True)
    r.set_defaults(fn=cmd_context)
    r = cs.add_parser("dismiss"); r.add_argument("commitment_id"); r.set_defaults(fn=cmd_context)
    r = cs.add_parser("recall")
    r.add_argument("query")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(fn=cmd_context)
    r = cs.add_parser("distill"); r.set_defaults(fn=cmd_context)
    r = cs.add_parser("remind"); r.set_defaults(fn=cmd_context)
    s = sub.add_parser("content")
    cts = s.add_subparsers(dest="content_cmd", required=True)
    r = cts.add_parser("draft")
    r.add_argument("--project", required=True)
    r.add_argument("--platform", required=True)
    r.add_argument("--request", required=True)
    r.add_argument("--drain", action="store_true")
    r.set_defaults(fn=cmd_content)
    r = cts.add_parser("list")
    r.add_argument("--status", default="ready", choices=["ready", "published", "cleared", "failed", "all"])
    r.add_argument("--queue-status", default="queued", choices=["queued", "drafted", "dead", "all"])
    r.add_argument("--limit", type=int, default=50)
    r.set_defaults(fn=cmd_content)
    r = cts.add_parser("publish")
    r.add_argument("draft_id")
    r.set_defaults(fn=cmd_content)
    r = cts.add_parser("drain"); r.set_defaults(fn=cmd_content)
    s = sub.add_parser("brief")
    bs = s.add_subparsers(dest="brief_cmd", required=True)
    r = bs.add_parser("ceo")
    r.add_argument("--notify", action="store_true")
    r.add_argument("--once-per-day", action="store_true")
    r.add_argument("--timezone", default="UTC")
    r.set_defaults(fn=cmd_brief)
    s = sub.add_parser("history"); s.add_argument("--team", required=True)
    s.add_argument("--days", type=int, default=7); s.set_defaults(fn=cmd_history)

    s = sub.add_parser("know"); ks = s.add_subparsers(dest="know_cmd", required=True)
    a = ks.add_parser("add"); a.add_argument("--scope", default="team")
    a.add_argument("--team"); a.add_argument("--title", required=True)
    a.add_argument("--content", required=True)
    q = ks.add_parser("search"); q.add_argument("--team", required=True)
    q.add_argument("query"); q.add_argument("--k", type=int, default=5)
    s.set_defaults(fn=cmd_know)
    return p


def main(argv=None) -> int:
    try:
        envfile.load_from_environment()
        args = build_parser().parse_args(argv)
        return args.fn(args)
    except (loader.ConfigError, ValidationError) as exc:
        print(f"argus: config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
