from pathlib import Path
from datetime import datetime, timezone

from argus.v2.queue import jobs
from argus.v2.worker import worker
from argus.v2.ingress import events
from argus.v2 import cli

FIX = Path(__file__).parent / "fixtures" / "argus.yaml"


def test_worker_prompt_includes_recent_history(conn, cfg):
    # An earlier message in the same conversation should reach the role prompt.
    eid = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                                text="remember: prod is on aws")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT conversation_id FROM events WHERE dedup_key='m1'")
        conv = str(cur.fetchone()[0])
    jobs.enqueue(conn, team_id="dev", kind="pipeline", role="developer", stage=0,
                 idempotency_key="k1", exec_snapshot={"engine": "echo", "prompt": "do it"},
                 payload={"text": "deploy"}, conversation_id=conv, event_id=str(eid))
    conn.commit()
    worker.run_once(cfg, "w1")
    with conn.cursor() as cur:
        cur.execute("SELECT prompt FROM runs ORDER BY started_at DESC LIMIT 1")
        prompt = cur.fetchone()[0]
    assert "prod is on aws" in prompt   # history assembled into the prompt
    assert "deploy" in prompt            # the task itself


def test_cli_summarize_and_history(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(FIX))
    events.ingest_message(conn, None, team="dev", source="cli", dedup_key="s1", text="hello")
    conn.commit()
    today = datetime.now(timezone.utc).date().isoformat()
    rc = cli.main(["summarize", "--team", "dev", "--day", today])
    assert rc == 0
    out = capsys.readouterr().out
    assert "message" in out
    rc = cli.main(["history", "--team", "dev", "--days", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert today in out
