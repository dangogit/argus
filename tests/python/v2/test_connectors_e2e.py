from pathlib import Path
import json

from argus.v2.connectors import driver
from argus.v2.config import loader
from argus.v2.orchestrator import reconcile
from argus.v2.worker import worker


def _cfg(tmp_path):
    y = tmp_path / "a.yaml"
    signals = [{"fingerprint": "ISSUE-1", "payload": {"title": "boom"}}]
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        f"    - {{ name: s, type: fake, scope: company, team: dev, config: {json.dumps({'signals': signals})} }}\n"
        "teams:\n  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa] }\n")
    return loader.load(y)


def test_polled_signal_runs_a_pipeline_to_done(conn, tmp_path):
    cfg = _cfg(tmp_path)
    assert driver.poll_once(conn, cfg) == 1            # ingest the signal
    for _ in range(8):                                  # drive the pipeline
        reconcile.sweep_once(conn, cfg); conn.commit()
        while worker.run_once(cfg, "w1"):
            pass
    with conn.cursor() as cur:
        cur.execute("SELECT status, fingerprint FROM requests")
        status, fp = cur.fetchone()
    assert status == "done" and fp == "ISSUE-1"
    # Re-poll: cursor advanced, no second request.
    assert driver.poll_once(conn, cfg) == 0
