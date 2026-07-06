from pathlib import Path
import plistlib

import pytest

from argus.v2 import cli
from argus.v2.queue import jobs
from argus.v2.queue.models import RunRecord

FIX = Path(__file__).parent / "fixtures" / "argus.yaml"


def test_submit_then_status(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(FIX))
    rc = cli.main(["submit", "--team", "dev", "fix the login bug"])
    assert rc == 0
    rc = cli.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "events" in out.lower()


def test_signal_injects_event(conn, pg_dsn, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(FIX))
    rc = cli.main(["signal", "--team", "dev", "--source", "sentry",
                   "--fingerprint", "ISSUE-1", '{"err":"boom"}'])
    assert rc == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE kind='signal'")
        assert cur.fetchone()[0] == 1


def test_launchd_render_cli_writes_python_units(tmp_path, monkeypatch, pg_dsn):
    monkeypatch.setenv("ARGUS_CONFIG", str(FIX))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    out = tmp_path / "units"
    rc = cli.main([
        "launchd", "render",
        "--out", str(out),
        "--python", "/venv/bin/python",
        "--run-root", "/run",
        "--env-file", "/secrets.env",
        "--log-dir", "/logs",
    ])
    assert rc == 0
    up = plistlib.loads((out / "com.argus.up.plist").read_bytes())
    assert up["ProgramArguments"] == [
        "/venv/bin/python", "-m", "argus.v2.cli", "up", "--sweep-only", "--poll", "10",
    ]
    chat = plistlib.loads((out / "com.argus.work-chat.plist").read_bytes())
    assert chat["ProgramArguments"][3:] == ["worker", "--lane", "chat", "--poll", "2"]
    assert up["EnvironmentVariables"]["ARGUS_CONFIG_V2"] == str(FIX)
    assert up["EnvironmentVariables"]["ARGUS_CONFIG"] == str(FIX)
    assert up["EnvironmentVariables"]["ARGUS_ENV_FILES"] == "/secrets.env"
    assert "ARGUS_DB_DSN" not in up["EnvironmentVariables"]


def test_serve_default_port_is_argus_inbound_port():
    parser = cli.build_parser()
    args = parser.parse_args(["serve"])
    assert args.port == 8787


def test_top_level_operational_parsers():
    parser = cli.build_parser()

    assert parser.parse_args(["version"]).fn is cli.cmd_version
    assert parser.parse_args(["init", "--config", "x.yaml"]).config == "x.yaml"
    assert parser.parse_args(["init", "--channel", "telegram"]).channel == "telegram"
    assert parser.parse_args(["init", "--channel", "slack"]).channel == "slack"
    assert parser.parse_args(["init", "--channel", "whatsapp"]).channel == "whatsapp"
    wow = parser.parse_args(["wow", "/tmp/x", "--channel", "whatsapp"])
    assert wow.fn is cli.cmd_wow and wow.channel == "whatsapp"
    onboard = parser.parse_args([
        "onboard", "project", "/tmp/x", "--mode", "monitor-only",
        "--channel", "whatsapp",
    ])
    assert onboard.fn is cli.cmd_onboard and onboard.mode == "monitor-only"
    assert onboard.channel == "whatsapp"
    assert parser.parse_args(["doctor", "--live"]).live is True
    doctor = parser.parse_args(["doctor", "--deep", "--json"])
    assert doctor.deep is True and doctor.json is True
    assert parser.parse_args(["ready"]).live is False
    go_live = parser.parse_args([
        "go-live", "--mode", "pm-propose-pr", "--skip-pr-smoke",
        "--fresh-slack-proof",
    ])
    assert go_live.fn is cli.cmd_go_live and go_live.skip_pr_smoke is True
    assert go_live.fresh_slack_proof is True
    assert parser.parse_args(["verify"]).fn is cli.cmd_verify
    assert parser.parse_args(["validate"]).fn is cli.cmd_validate
    assert parser.parse_args(["validate-roles"]).fn is cli.cmd_validate_roles
    assert parser.parse_args(["db", "migrate"]).db_cmd == "migrate"
    assert parser.parse_args(["projects", "list"]).projects_cmd == "list"
    assert parser.parse_args(["config", "get", "company.name"]).key == "company.name"
    inbound = parser.parse_args(["inbound", "handle", "--channel", "telegram"])
    assert inbound.fn is cli.cmd_inbound and inbound.label == "inbound"
    wa = parser.parse_args(["wa", "handle"])
    assert wa.fn is cli.cmd_inbound and wa.label == "wa"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "argus 0.2.0"


def test_init_channel_telegram_writes_inbound_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "argus.yaml"

    assert cli.main(["init", "--config", str(out), "--channel", "telegram"]) == 0

    text = out.read_text(encoding="utf-8")
    assert 'webhook_secret: "${env:ARGUS_WEBHOOK_SECRET}"' in text
    assert "type: telegram" in text
    assert 'channel_id: "12345"' in text
    assert 'secret_ref: "${env:TELEGRAM_BOT_TOKEN}"' in text


def test_init_channel_slack_writes_inbound_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "argus.yaml"

    assert cli.main(["init", "--config", str(out), "--channel", "slack"]) == 0

    text = out.read_text(encoding="utf-8")
    assert 'webhook_secret: "${env:ARGUS_WEBHOOK_SECRET}"' in text
    assert "type: slack" in text
    assert 'channel_id: "C1234567890"' in text
    assert 'secret_ref: "${env:SLACK_BOT_TOKEN}"' in text
    assert 'signing_secret: "${env:SLACK_SIGNING_SECRET}"' in text


def test_init_channel_whatsapp_writes_inbound_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "argus.yaml"

    assert cli.main(["init", "--config", str(out), "--channel", "whatsapp"]) == 0

    text = out.read_text(encoding="utf-8")
    assert 'webhook_secret: "${env:ARGUS_WEBHOOK_SECRET}"' in text
    assert "type: whatsapp" in text
    assert 'channel_id: "120363_REPLACE_ME@g.us"' in text
    assert 'secret_ref: "${env:ARGUS_WA_APIKEY}"' in text
    assert 'base_url: "${env:ARGUS_WA_URL}"' in text
    assert 'instance: "${env:ARGUS_WA_INSTANCE}"' in text


def test_config_error_prints_without_traceback(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n"
        "    channels: [ { type: telegram, role: control, channel_id: '123', "
        "secret_ref: TELEGRAM_BOT_TOKEN } ]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ARGUS_CONFIG", raising=False)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(bad))

    assert cli.main(["validate"]) == 2

    err = capsys.readouterr().err
    assert "argus: config error: bad secret ref: 'TELEGRAM_BOT_TOKEN'" in err
    assert "Traceback" not in err


def test_doctor_and_ready_fail_unsupported_channel(tmp_path, monkeypatch, pg_dsn, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: t\n"
        "    roles: [ { name: r, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [r] }\n"
        "    channels: [ { type: mastodon, role: control, channel_id: C123 } ]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ARGUS_CONFIG", raising=False)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(bad))
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    assert cli.main(["doctor", "--live"]) == 1
    assert cli.main(["ready", "--live"]) == 1

    out = capsys.readouterr().out
    assert "[fail] config team t: unsupported channel type 'mastodon'" in out


def test_db_migrate_is_idempotent(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    assert cli.main(["db", "migrate"]) == 0

    assert capsys.readouterr().out.strip() == "db migrate: applied=0"


def test_verify_reports_source_checkout_required(monkeypatch, tmp_path, capsys):
    fake_pkg = tmp_path / "site-packages" / "argus" / "v2"
    fake_pkg.mkdir(parents=True)
    fake_cli = fake_pkg / "cli.py"
    fake_cli.write_text("# installed copy\n", encoding="utf-8")
    monkeypatch.setattr(cli, "__file__", str(fake_cli))

    assert cli.main(["verify"]) == 2

    assert "verify: source checkout required (missing scripts/gate.py)" in capsys.readouterr().err


def test_config_get_projects_and_validate(monkeypatch, pg_dsn, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(FIX))

    assert cli.main(["config", "get", "company.name"]) == 0
    assert cli.main(["projects", "list"]) == 0
    assert cli.main(["validate"]) == 0
    assert cli.main(["validate-roles"]) == 0

    out = capsys.readouterr().out
    assert "testco" in out
    assert "dev" in out
    assert "validate: ok teams=1" in out
    assert "validate-roles: ok" in out


def test_config_convert_cli_writes_valid_v2_config(tmp_path, capsys):
    legacy = tmp_path / "argus.config.yaml"
    legacy.write_text("engine:\n  default: echo\n", encoding="utf-8")
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text("name: demo\nrepo: /repo/demo\n", encoding="utf-8")
    out_file = tmp_path / "argus.yaml"

    assert cli.main([
        "config", "convert",
        "--input", str(legacy),
        "--projects-dir", str(tmp_path / "projects"),
        "--out", str(out_file),
    ]) == 0

    assert capsys.readouterr().out.strip() == str(out_file)
    assert "name: demo" in out_file.read_text(encoding="utf-8")


def test_support_run_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_support", lambda args: calls.append(args.team) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["support", "run", "--team", "luma"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == ["luma"]


def test_support_list_dry_reply_and_clear(conn, pg_dsn, monkeypatch, capsys):
    from argus.v2.support import state

    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    draft = state.register_draft("luma", "T1", "u@example.com", "Help", "Reply body", "apps")

    assert cli.main(["support", "list", "--team", "luma"]) == 0
    assert cli.main(["support", "reply", "--team", "luma", draft.id, "--dry-run"]) == 0
    assert cli.main(["support", "clear", "--team", "luma", draft.id]) == 0

    out = capsys.readouterr().out
    assert draft.id in out
    assert "Reply body" in out
    assert f"support luma: cleared {draft.id}" in out
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM support_drafts WHERE id=%s", (draft.id,))
        assert cur.fetchone()[0] == "cleared"


def test_pm_pending_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_pm", lambda args: calls.append((args.notify, args.team)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["pm", "pending", "--notify", "dev"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [(True, ["dev"])]


def test_pm_run_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_pm",
                        lambda args: calls.append((args.team, args.fingerprint, args.message)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["pm", "run", "dev", "F1", "--message", "fix login"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("dev", "F1", "fix login")]


def test_pm_cycle_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_pm", lambda args: calls.append(args.team) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["pm", "cycle", "dev"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == ["dev"]


def test_retro_run_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_retro",
                        lambda args: calls.append((args.retro_cmd, args.date,
                                                   args.team, args.company_only,
                                                   args.no_notify)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args([
        "retro", "run", "--date", "2026-06-18", "--team", "dev", "--no-notify",
    ])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("run", "2026-06-18", "dev", False, True)]


def test_retro_run_company_only_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_retro",
                        lambda args: calls.append((args.retro_cmd, args.company_only)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["retro", "run", "--company-only"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("run", True)]


def test_retro_backlog_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_retro",
                        lambda args: calls.append((args.retro_cmd, args.status,
                                                   args.team)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["retro", "backlog", "--status", "gated", "--team", "__company__"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("backlog", "gated", "__company__")]


def test_retro_notify_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_retro",
                        lambda args: calls.append((args.retro_cmd, args.date,
                                                   args.team, args.company_only)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args([
        "retro", "notify", "--date", "2026-06-18", "--team", "dev",
    ])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("notify", "2026-06-18", "dev", False)]


def test_state_import_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_state",
                        lambda args: calls.append((args.state_cmd, args.run_root, args.dry_run)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["state", "import", "--run-root", "/run", "--dry-run"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("import", "/run", True)]


def test_assistant_memory_refresh_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_assistant",
                        lambda args: calls.append((args.assistant_cmd, args.memory_cmd)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["assistant", "memory", "refresh"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("memory", "refresh")]


def test_advisor_tick_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_advisor", lambda args: calls.append(args.advisor_cmd) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["advisor", "tick"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == ["tick"]


def test_advisor_digest_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_advisor", lambda args: calls.append(args.advisor_cmd) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["advisor", "digest"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == ["digest"]


def test_advisor_ingest_and_status(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    assert cli.main([
        "advisor", "ingest",
        "--group", "120@g.us",
        "--message-id", "M1",
        "--participant", "111",
        "--mentioned", "222",
        "--ts", "1000",
        "hello",
    ]) == 0
    assert cli.main(["advisor", "status"]) == 0

    out = capsys.readouterr().out
    assert "advisor message M1" in out
    assert "120@g.us\tcursor=0\tmessages=1\treplies=0\tdigests=0" in out


def test_wa_handle_ingests_payload(conn, pg_dsn, monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "argus.yaml"
    cfg.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo }, webhook_secret: s3cret }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: chatA } ]\n"
    )
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(cfg))

    assert cli.main([
        "wa", "handle",
        "--channel", "fake",
        '{"chat_id":"chatA","id":"m1","text":"fix login"}',
    ]) == 0

    out = capsys.readouterr().out
    assert "wa handle: status=200 ingested=1" in out
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM events WHERE dedup_key='m1'")
        assert cur.fetchone()[0] == "fix login"


def test_host_render_cli_writes_manifest_units(tmp_path, capsys):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "poll.yaml").write_text(
        "name: poller\n"
        "kind: schedule\n"
        "command: argus poll\n"
        "interval: 300\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "units"

    assert cli.main([
        "host", "render",
        "--os", "linux",
        "--jobs-dir", str(jobs),
        "--out", str(out_dir),
    ]) == 0

    out = capsys.readouterr().out
    assert "argus-poller.service" in out
    assert (out_dir / "argus-poller.timer").exists()


def test_calendar_cli_calls_v2_calendar(monkeypatch, capsys):
    from argus.v2 import calendar

    calls = []
    monkeypatch.setattr(
        calendar,
        "run",
        lambda command, params, json_output=False: calls.append((command, params, json_output)) or "ok",
    )

    assert cli.main([
        "calendar", "create",
        "--title", "Call",
        "--start", "2026-06-18T09:00:00+03:00",
        "--duration", "30",
        "--json",
    ]) == 0

    assert capsys.readouterr().out == "ok\n"
    assert calls == [("create", {
        "title": "Call",
        "start": "2026-06-18T09:00:00+03:00",
        "duration_min": 30,
    }, True)]


def test_context_distill_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_context", lambda args: calls.append(args.context_cmd) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["context", "distill"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == ["distill"]


def test_context_remind_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_context", lambda args: calls.append(args.context_cmd) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["context", "remind"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == ["remind"]


def test_context_commitment_cli(conn, pg_dsn, monkeypatch, capsys):
    from argus.v2.context import state

    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    commit_id = state.commit_upsert("daniel", "ship v2", "2026-06-19", "cli-test")

    assert cli.main(["context", "status"]) == 0
    assert cli.main(["context", "commitments"]) == 0
    assert cli.main(["context", "snooze", commit_id, "--until", "2026-06-20T09:00:00Z"]) == 0
    assert cli.main(["context", "done", commit_id]) == 0
    assert cli.main(["context", "recall", "ship"]) == 0

    out = capsys.readouterr().out
    assert "commitments\topen\t1" in out
    assert f"{commit_id}\topen\t2026-06-19\tdaniel\tship v2" in out
    assert f"context snooze: {commit_id}" in out
    assert f"context done: {commit_id}" in out
    assert f"commitment\t{commit_id}\tdone\t2026-06-19\tdaniel\tship v2" in out


def test_content_drain_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_content", lambda args: calls.append(args.content_cmd) or 0)

    parser = cli.build_parser()
    args = parser.parse_args(["content", "drain"])
    rc = args.fn(args)

    assert rc == 0
    assert calls == ["drain"]


def test_content_draft_list_and_publish(conn, pg_dsn, monkeypatch, capsys):
    from argus.v2.actions import handlers
    from argus.v2.content import state

    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    assert cli.main([
        "content", "draft",
        "--project", "luma",
        "--platform", "linkedin",
        "--request", "announce launch",
    ]) == 0
    draft_id = state.register("luma", "linkedin")
    publish_payloads = []
    monkeypatch.setattr(
        handlers, "run",
        lambda action_type, payload: publish_payloads.append(payload) or "posted:1",
    )

    assert cli.main(["content", "list"]) == 0
    assert cli.main([
        "content", "publish", draft_id,
        "--approval-proof", "owner approved content-approval:pr:2:publish:1783099493.667819",
        "--durable-media", "image stored in content draft",
        "--cta-route", "cta link checked",
        "--dm-activation", "dm active",
        "--metricool-target", "Metricool target selected",
        "--connector-auth", "publisher auth checked",
    ]) == 0

    out = capsys.readouterr().out
    assert "content queue " in out
    assert f"draft\t{draft_id}\tluma\tlinkedin\tready" in out
    assert "queue\t" in out
    assert "content publish: posted:1" in out
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM content_drafts WHERE id=%s", (draft_id,))
        assert cur.fetchone()[0] == "published"
    assert publish_payloads[0]["live_readiness"]["approval_proof"].startswith("owner approved")
    assert publish_payloads[0]["live_readiness"]["metricool_target"] == "Metricool target selected"


def test_ceo_brief_parser(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_brief",
                        lambda args: calls.append((args.brief_cmd, args.notify,
                                                   args.once_per_day, args.timezone)) or 0)

    parser = cli.build_parser()
    args = parser.parse_args([
        "brief", "ceo", "--notify", "--once-per-day", "--timezone", "UTC",
    ])
    rc = args.fn(args)

    assert rc == 0
    assert calls == [("ceo", True, True, "UTC")]


def test_poll_dry_run_reports_counts_without_ingesting(tmp_path, conn, pg_dsn,
                                                       monkeypatch, capsys):
    cfg = tmp_path / "argus.yaml"
    cfg.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - name: src1\n"
        "      type: fake\n"
        "      scope: company\n"
        "      team: dev\n"
        "      config:\n"
        "        signals:\n"
        "          - { fingerprint: ISSUE-1 }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
    )
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(cfg))

    rc = cli.main(["poll", "--dry-run", "--source", "src1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "src1\tdev\tfake\tok\t1\tidx" in out
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events")
        assert cur.fetchone()[0] == 0


def _make_dead_job(conn, *, idempotency_key):
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                idempotency_key=idempotency_key, exec_snapshot={"engine": "echo"},
                payload={}, max_attempts=1)
    conn.commit()
    job = jobs.claim(conn, "w1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=now() - interval '1 min' WHERE id=%s",
                    (job.id,))
    conn.commit()
    jobs.reclaim_expired(conn); conn.commit()
    return job.id


def test_dead_job_list_shows_dead_job(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(FIX))
    job_id = _make_dead_job(conn, idempotency_key="cli-dead-1")

    rc = cli.main(["dead-job", "list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert job_id in out
    assert "dev" in out
    assert "pipeline" in out


def test_dead_job_retry_makes_job_claimable_and_completable(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(FIX))
    job_id = _make_dead_job(conn, idempotency_key="cli-dead-2")

    rc = cli.main(["dead-job", "retry", job_id])
    assert rc == 0
    out = capsys.readouterr().out
    assert "retry" in out.lower()

    with conn.cursor() as cur:
        cur.execute("SELECT status, attempts, max_attempts FROM jobs WHERE id=%s", (job_id,))
        status, attempts, max_attempts = cur.fetchone()
    assert status == "pending"
    assert attempts == 0
    assert max_attempts == 2  # bumped so it can survive one more failed attempt

    # Now claimable and completable, like any normal job.
    job = jobs.claim(conn, "w2"); conn.commit()
    assert job.id == job_id
    ok = jobs.finalize(conn, job.id, job.claim_token, status="done", result={},
                       run=RunRecord(role="developer", engine="echo", status="ok"),
                       actions=[])
    conn.commit()
    assert ok is True
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE id=%s", (job.id,))
        assert cur.fetchone()[0] == "done"


def test_dead_job_retry_unknown_id_fails(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(FIX))
    rc = cli.main(["dead-job", "retry", "00000000-0000-0000-0000-000000000000"])
    assert rc == 1
