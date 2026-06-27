import json

from argus.v2 import state_import


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_state_import_loads_legacy_run_root(tmp_path, conn):
    root = tmp_path / "run"
    _write_jsonl(root / "alerts.jsonl", [
        {
            "ts": "2026-06-18T10:00:00Z",
            "severity": "warn",
            "project": "legacy",
            "fingerprint": "a1",
            "message": "old alert",
            "channel": "log",
        },
    ])
    advisor = root / "advisor" / "120363000000000001"
    _write_jsonl(advisor / "messages.jsonl", [
        {"id": "M1", "ts": 1000, "participant": "111", "push_name": "Alice", "body": "hello"},
    ])
    _write_jsonl(advisor / "replies.jsonl", [
        {"ts": 1001, "participant": "111", "reply_to_id": "M1", "parts": 1},
    ])
    _write_jsonl(advisor / "digests.jsonl", [
        {"date": "2026-06-17", "message_count": 1, "posted": False, "reason": "quiet"},
    ])
    (advisor / "cursor").write_text("1\n", encoding="utf-8")
    _write_jsonl(root / "content" / "index.jsonl", [
        {"id": "draft-1", "ts": "2026-06-18T10:00:00Z", "project": "p", "platform": "x", "status": "ready"},
    ])
    _write_jsonl(root / "content" / "queue.jsonl", [
        {"id": "queue-1", "ts": "2026-06-18T10:00:00Z", "project": "p", "platform": "x", "request": "post", "status": "queued", "attempts": 1},
    ])
    _write_jsonl(root / "context" / "watermarks.jsonl", [
        {"job": "distill", "source": "sqlite", "last_id": 7, "ts": "2026-06-18T10:00:00Z"},
    ])
    _write_jsonl(root / "context" / "commitments.jsonl", [
        {"id": "commit-1", "what": "send report", "status": "open", "source_ref": "m1"},
    ])
    support = root / "support" / "p"
    _write_jsonl(support / "threads.jsonl", [
        {"thread_id": "T1", "action": "draft_ready", "from": "u@example.com", "subject": "Help"},
    ])
    _write_jsonl(support / "drafts" / "index.jsonl", [
        {"id": "draft-s", "thread_id": "T1", "from": "u@example.com", "subject": "Help", "status": "ready"},
    ])
    draft_dir = support / "drafts" / "draft-s"
    draft_dir.mkdir(parents=True)
    (draft_dir / "reply.txt").write_text("Reply", encoding="utf-8")
    (draft_dir / "email.json").write_text(
        json.dumps({"thread_id": "T1", "from": "u@example.com", "subject": "Help", "transport": "apps"}),
        encoding="utf-8",
    )
    guidance_dir = support / "guidance" / "g1"
    guidance_dir.mkdir(parents=True)
    (guidance_dir / "request.json").write_text(
        json.dumps({
            "id": "g1",
            "project": "p",
            "thread_id": "T1",
            "from": "u@example.com",
            "subject": "Help",
            "question": "q",
            "proposed_reply": "r",
            "thread": "thread",
            "status": "pending",
        }),
        encoding="utf-8",
    )
    _write_jsonl(root / "assistant" / "history.jsonl", [
        {"role": "owner", "text": "remember this", "ts": "2026-06-18T10:00:00Z"},
    ])
    (root / "assistant" / "memory.md").write_text("Memory\n", encoding="utf-8")
    (root / "assistant" / "memory.watermark").write_text("1\n", encoding="utf-8")

    counts = state_import.run(conn, run_root=root)
    conn.commit()

    assert counts == {
        "alerts": 1,
        "advisor": 4,
        "content": 2,
        "context": 2,
        "support": 3,
        "assistant": 2,
    }
    with conn.cursor() as cur:
        cur.execute("SELECT message FROM alerts WHERE fingerprint='a1'")
        assert cur.fetchone()[0] == "old alert"
        cur.execute("SELECT body FROM advisor_messages WHERE message_id='M1'")
        assert cur.fetchone()[0] == "hello"
        cur.execute("SELECT value FROM advisor_cursors WHERE jid='120363000000000001@g.us'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT request FROM content_queue WHERE id='queue-1'")
        assert cur.fetchone()[0] == "post"
        cur.execute("SELECT last_id FROM context_watermarks WHERE job='distill'")
        assert cur.fetchone()[0] == 7
        cur.execute("SELECT reply FROM support_drafts WHERE id='draft-s'")
        assert cur.fetchone()[0] == "Reply"
        cur.execute("SELECT content FROM assistant_memory WHERE name='default'")
        assert cur.fetchone()[0] == "Memory\n"
