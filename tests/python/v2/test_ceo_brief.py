from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from argus.v2.brief import ceo
from argus.v2.config import loader


def _cfg(tmp_path):
    y = tmp_path / "ceo.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: ceo-brief\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - { type: whatsapp, role: control, channel_id: 'ceo@g.us' }\n")
    return loader.load(y)


def _cfg_with_projects(tmp_path):
    dev = tmp_path / "dev"
    quiet = tmp_path / "quiet"
    dev.mkdir()
    quiet.mkdir()
    y = tmp_path / "ceo-projects.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: ceo-brief\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - { type: whatsapp, role: control, channel_id: 'ceo@g.us' }\n"
        "  - name: dev\n"
        f"    project: {{ repo: {dev}, base_branch: main }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "  - name: quiet\n"
        f"    project: {{ repo: {quiet}, base_branch: main }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n")
    return loader.load(y)


def test_ceo_brief_reports_health_and_pending_counts(conn, tmp_path):
    cfg = _cfg(tmp_path)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (team_id, role, kind, status, idempotency_key) "
            "VALUES ('ceo-brief','manager','pipeline','dead','j1')")
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, status, idempotency_key, payload) "
            "VALUES ('ceo-brief','notify','reversible_internal','proposed','a1',%s)",
            (Json({"text": "x"}),),
        )
    brief = ceo.build(conn, cfg, health_lines=["com.argus.up loaded"])
    assert "Overall: needs attention" in brief.text
    assert "Recent failed/dead agents: 1" in brief.text
    assert "- Inspect 1 recent failed/dead agent(s)" in brief.text
    assert "Actions pending/approval: 1" in brief.text
    assert "- com.argus.up loaded" in brief.text


def test_ceo_brief_ignores_cancelled_and_old_failed_jobs(conn, tmp_path):
    cfg = _cfg(tmp_path)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (team_id, kind, source, dedup_key, payload) "
            "VALUES ('ceo-brief','signal','test','cancelled-job','{}') RETURNING id")
        event_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO requests (event_id, team_id, status, fingerprint) "
            "VALUES (%s,'ceo-brief','cancelled','cancelled-job') RETURNING id",
            (event_id,),
        )
        request_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO jobs (request_id, team_id, role, kind, status, idempotency_key) "
            "VALUES (%s,'ceo-brief','manager','pipeline','dead','cancelled-dead')",
            (request_id,),
        )
        cur.execute(
            "INSERT INTO jobs (team_id, role, kind, status, idempotency_key, updated_at) "
            "VALUES ('ceo-brief','manager','pipeline','failed','old-failed', now() - interval '3 days')")

    brief = ceo.build(conn, cfg, health_lines=["com.argus.up loaded"])

    assert "Overall: healthy" in brief.text
    assert "Recent failed/dead agents: 0" in brief.text


def test_ceo_brief_reports_action_only_agent_visibility(conn, tmp_path):
    cfg = _cfg_with_projects(tmp_path)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (team_id, kind, source, dedup_key, payload) "
            "VALUES ('dev','signal','test','dev1','{}')")
        cur.execute(
            "INSERT INTO events (team_id, kind, source, dedup_key, payload) "
            "VALUES ('quiet','signal','test','quiet1','{}')")
        cur.execute(
            "INSERT INTO support_drafts (id, project, thread_id, sender, subject) "
            "VALUES ('d1','dev','t1','s','needs reply')")
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, status, idempotency_key, "
            "provider_ref, updated_at) "
            "VALUES ('quiet','open_pr','reversible_internal','done','pr1',"
            "'https://github.com/o/r/pull/1', now() - interval '2 days')")

    brief = ceo.build(conn, cfg, runner=lambda _argv, _cwd: "[]",
                      health_lines=["com.argus.up loaded"])

    assert "Overall: pending review" in brief.text
    assert "Agent visibility:" in brief.text
    assert "- Action: dev: 1 support draft(s) ready" in brief.text
    assert "- Quiet: 1 project agent(s) checked, 1 PR(s) opened last 7d, no action required" in brief.text
    assert "- Action: dev: 1 support draft(s) ready" in brief.text.split("Top priorities:", 1)[1]


def test_ceo_notify_inserts_whatsapp_action(conn, tmp_path):
    cfg = _cfg(tmp_path)
    brief = ceo.Brief("hello", 0, 0, 0, 0)
    assert ceo.notify(conn, cfg, brief, key="ceo:test") is True
    assert ceo.notify(conn, cfg, brief, key="ceo:test") is False
    with conn.cursor() as cur:
        cur.execute("SELECT destination_ref, payload->>'text' FROM actions WHERE idempotency_key='ceo:test'")
        assert cur.fetchone() == ("whatsapp:ceo@g.us", "hello")


def test_ceo_once_per_day_window(tmp_path):
    now = datetime(2026, 6, 18, 9, 5, tzinfo=ZoneInfo("UTC"))
    assert ceo.should_send_once(now=now, timezone="UTC", run_root=tmp_path) is True
    assert ceo.should_send_once(now=now, timezone="UTC", run_root=tmp_path) is False
    late = datetime(2026, 6, 19, 9, 45, tzinfo=ZoneInfo("UTC"))
    assert ceo.should_send_once(now=late, timezone="UTC", run_root=tmp_path) is False


def test_launchd_row_health_treats_running_jobs_as_ok():
    assert ceo._launchd_row_needs_attention("123\t1\tcom.argus.evolution") is False
    assert ceo._launchd_row_needs_attention("-\t0\tcom.argus.poll") is False
    assert ceo._launchd_row_needs_attention("-\t1\tcom.argus.watchdog") is True
