from psycopg.types.json import Json

from argus.v2.actions import executor
from argus.v2.config import loader


def _cfg(tmp_path, *, personal_auto=True):
    y = tmp_path / "personal.yaml"
    mode = "auto" if personal_auto else "approval"
    y.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    autonomy: { reversible_internal: auto, personal_outward: approval, irreversible_outward: approval }\n"
        "teams:\n"
        "  - name: personal\n"
        f"    autonomy: {{ reversible_internal: auto, personal_outward: {mode}, irreversible_outward: approval }}\n"
        "    roles: [ { name: manager, kind: front, prompt: p, engine: { engine: echo } } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels: [ { type: cli, role: control, channel_id: local } ]\n")
    return loader.load(y)


def _insert_action(conn, action_type, payload):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, idempotency_key, payload) "
            "VALUES ('personal', %s, 'irreversible_outward', %s, %s) RETURNING id",
            (action_type, f"k:{action_type}", Json(payload)),
        )
        return str(cur.fetchone()[0])


def test_personal_outward_auto_runs_content_queue(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    cfg = _cfg(tmp_path, personal_auto=True)
    aid = _insert_action(conn, "content_queue", {
        "project": "personal",
        "platform": "linkedin",
        "brief": "write launch note",
    })
    executor.process_proposed(conn, cfg)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, risk, provider_ref FROM actions WHERE id=%s", (aid,))
        status, risk, ref = cur.fetchone()
    assert (status, risk) == ("done", "personal_outward")
    assert ref.startswith("content:queue:")


def test_personal_outward_defaults_to_approval(conn, tmp_path):
    cfg = _cfg(tmp_path, personal_auto=False)
    calls = []
    _insert_action(conn, "calendar_create", {
        "title": "Call",
        "start": "2026-06-18T09:00:00+03:00",
    })
    executor.process_proposed(conn, cfg, runner=lambda argv, cwd=None: calls.append(argv) or "ok")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, risk FROM actions WHERE type='calendar_create'")
        status, risk = cur.fetchone()
    assert (status, risk) == ("awaiting_approval", "personal_outward")
    assert calls == []


def test_personal_email_fails_closed_when_unconfigured(conn, tmp_path):
    cfg = _cfg(tmp_path, personal_auto=True)
    aid = _insert_action(conn, "email_reply", {
        "thread_id": "t1",
        "body": "reply",
    })
    executor.process_proposed(conn, cfg)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, payload->>'error' FROM actions WHERE id=%s", (aid,))
        status, error = cur.fetchone()
    assert status == "failed"
    assert "email transport not configured" in error
