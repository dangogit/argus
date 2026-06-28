"""Live self-updating status line (orchestrator/status.py + the channel edit
seam + the executor status action). The fake channel records send() in SENT and
update() in EDITED, so we can assert the message is edited in place across the
work lifecycle instead of spamming a new message per stage.

Production timing (the orchestrator sweeps on a timer while the worker runs
separately) is reproduced by draining the receipt with process_proposed BEFORE
running the worker, then looping worker -> sweep so each stage edit flushes.
"""
import json

import pytest

from argus.v2.actions import executor
from argus.v2.channels import fake, send
from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline, reconcile, status
from argus.v2.worker import worker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_fake():
    fake.SENT.clear()
    fake.EDITED.clear()
    yield
    fake.SENT.clear()
    fake.EDITED.clear()


@pytest.fixture()
def cfg_status(tmp_path):
    """Conversational team on an EDIT-CAPABLE (fake) control channel, so the
    receipt becomes a live status line."""
    y = tmp_path / "s.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles:\n"
        "      - name: manager\n"
        "        kind: front\n"
        "        prompt: 'mgr'\n"
        "        engine: { engine: scripted }\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
        "    channels:\n"
        "      - { type: fake, role: control, channel_id: grp1 }\n"
    )
    return loader.load(y)


def _ingest(conn, cfg, *, text="what are the open PRs?", key="s1"):
    return events.ingest_message(conn, cfg, team="dev", source="fake",
                                 dedup_key=key, text=text, conversation_key="fake:grp1")


def _scripted(action, reply="", task=""):
    return f'ARGUS_RESULT: {json.dumps({"action": action, "reply": reply, "task": task})}'


def _edited_texts():
    return [t for (_chan, _mid, t) in fake.EDITED]


def _drive(conn, cfg, rounds=6):
    """Receipt first (production timing), then worker->sweep per round."""
    reconcile.route_events(conn, cfg); conn.commit()
    executor.process_proposed(conn, cfg); conn.commit()  # send the receipt now
    for _ in range(rounds):
        while worker.run_once(cfg, "w1"):
            pass
        reconcile.sweep_once(conn, cfg); conn.commit()


# ---------------------------------------------------------------------------
# Channel edit seam (unit)
# ---------------------------------------------------------------------------

def test_fake_channel_supports_edit():
    assert send.channel_supports_edit("fake") is True


def test_whatsapp_and_email_do_not_support_edit():
    assert send.channel_supports_edit("whatsapp") is False
    assert send.channel_supports_edit("email") is False


def test_send_edit_routes_to_update(cfg_status):
    out = send.edit(cfg_status, "fake:grp1", "fake-7", "edited text")
    assert out == "fake-7"
    assert fake.EDITED == [("grp1", "fake-7", "edited text")]


def test_send_edit_none_for_unknown_channel(cfg_status):
    assert send.edit(cfg_status, "nope:grp1", "m1", "x") is None
    assert send.edit(cfg_status, "fake:grp1", "", "x") is None  # no message id


# ---------------------------------------------------------------------------
# Receipt becomes a status action and is sent once
# ---------------------------------------------------------------------------

def test_receipt_posts_status_action_not_reply(conn, cfg_status, monkeypatch):
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _scripted("ignore")})
    eid = _ingest(conn, cfg_status); conn.commit()
    reconcile.route_events(conn, cfg_status); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT type, destination_ref, payload->>'text' FROM actions "
                    "WHERE idempotency_key=%s", (f"status:{eid}",))
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s", (f"ack:{eid}",))
        ack = cur.fetchone()[0]
    assert row is not None and row[0] == "status"
    assert row[1] == "fake:grp1"
    assert row[2] == status.RECEIPT
    assert ack == 0  # editable channel uses a status line, not a one-shot ack


def test_status_first_drain_sends_and_records_message_id(conn, cfg_status, monkeypatch):
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _scripted("ignore")})
    eid = _ingest(conn, cfg_status); conn.commit()
    reconcile.route_events(conn, cfg_status); conn.commit()
    executor.process_proposed(conn, cfg_status); conn.commit()
    assert ("grp1", status.RECEIPT) in fake.SENT
    with conn.cursor() as cur:
        cur.execute("SELECT status, provider_ref FROM actions WHERE idempotency_key=%s",
                    (f"status:{eid}",))
        st, ref = cur.fetchone()
    assert st == "done" and ref == "fake-1"


# ---------------------------------------------------------------------------
# Lifecycle: dispatch edits the SAME message working -> reviewing -> done
# ---------------------------------------------------------------------------

def test_dispatch_lifecycle_edits_in_place(conn, cfg_status, monkeypatch):
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _scripted(
                            "dispatch", reply="On it!", task="Fix the login redirect.")})
    _ingest(conn, cfg_status); conn.commit()
    _drive(conn, cfg_status)

    # The receipt was sent once; every later stage was an EDIT of fake-1.
    assert fake.SENT.count(("grp1", status.RECEIPT)) == 1
    assert all(mid == "fake-1" for (_c, mid, _t) in fake.EDITED), fake.EDITED
    # Distinct progression: working -> reviewing -> done (qa/senior collapse to one).
    distinct = []
    for t in _edited_texts():
        if not distinct or distinct[-1] != t:
            distinct.append(t)
    assert distinct == [status.WORKING, status.REVIEWING, status.DONE]


def test_answer_lifecycle_resolves_to_done(conn, cfg_status, monkeypatch):
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _scripted("answer", reply="3 open PRs.")})
    _ingest(conn, cfg_status); conn.commit()
    _drive(conn, cfg_status)
    # Status edited to DONE; the actual answer arrived as its own message.
    assert status.DONE in _edited_texts()
    assert ("grp1", "3 open PRs.") in fake.SENT


def test_status_edit_idempotent_no_duplicate(conn, cfg_status, monkeypatch):
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _scripted("ignore")})
    _ingest(conn, cfg_status); conn.commit()
    _drive(conn, cfg_status)
    edits_after_first = list(fake.EDITED)
    # Extra sweeps must not re-edit (text unchanged -> no re-arm).
    reconcile.sweep_once(conn, cfg_status); conn.commit()
    reconcile.sweep_once(conn, cfg_status); conn.commit()
    assert fake.EDITED == edits_after_first


# ---------------------------------------------------------------------------
# Toggle off + non-editable channel fall back to the one-shot receipt
# ---------------------------------------------------------------------------

def test_show_progress_false_uses_plain_reply(conn, cfg_status, monkeypatch):
    cfg_status.company.defaults.notifications.show_progress = False
    monkeypatch.setattr(pipeline, "_role_snapshot_extra",
                        lambda r: {"scripted_output": _scripted("ignore")})
    eid = _ingest(conn, cfg_status); conn.commit()
    reconcile.route_events(conn, cfg_status); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s", (f"status:{eid}",))
        st = cur.fetchone()[0]
        cur.execute("SELECT type FROM actions WHERE idempotency_key=%s", (f"ack:{eid}",))
        ack = cur.fetchone()
    assert st == 0, "no status action when progress disabled"
    assert ack is not None and ack[0] == "reply"


def test_non_editable_channel_uses_plain_reply(conn, cfg, monkeypatch):
    """cfg fixture has no manager engine; a work message dispatches via the rule
    path onto a whatsapp conversation (non-editable) -> one-shot ack, no status."""
    eid = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="wa1",
                                text="fix the login bug", conversation_key="whatsapp:grp9")
    conn.commit()
    reconcile.route_events(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE idempotency_key=%s", (f"status:{eid}",))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT type FROM actions WHERE idempotency_key=%s", (f"ack:{eid}",))
        assert cur.fetchone()[0] == "reply"


# ---------------------------------------------------------------------------
# Best-effort: a failed status edit never breaks the drain
# ---------------------------------------------------------------------------

def test_status_edit_failure_is_best_effort(conn, cfg_status, monkeypatch):
    eid = _ingest(conn, cfg_status); conn.commit()
    reconcile.route_events(conn, cfg_status); conn.commit()
    executor.process_proposed(conn, cfg_status); conn.commit()  # receipt sent, provider_ref set
    # Advance the line, then make the edit seam raise.
    status.set_status(conn, str(eid), status.WORKING); conn.commit()

    def boom(*a, **k):
        raise RuntimeError("slack down")

    monkeypatch.setattr(send, "edit", boom)
    monkeypatch.setattr(send, "deliver", boom)  # fallback also down
    executor.process_proposed(conn, cfg_status); conn.commit()  # must not raise
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key=%s", (f"status:{eid}",))
        assert cur.fetchone()[0] == "done"  # settled, not stuck/aborted


def test_set_status_noop_when_no_status_message(conn, cfg_status):
    # No status action exists for this event -> set_status changes nothing.
    eid = _ingest(conn, cfg_status); conn.commit()
    status.set_status(conn, str(eid), status.WORKING); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='status'")
        assert cur.fetchone()[0] == 0
