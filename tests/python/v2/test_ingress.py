from pathlib import Path

from argus.v2.ingress import events, media


def test_message_dedup_on_source_key(conn, cfg):
    a = events.ingest_message(conn, cfg, team="dev", source="cli",
                              dedup_key="m1", text="hi"); conn.commit()
    b = events.ingest_message(conn, cfg, team="dev", source="cli",
                              dedup_key="m1", text="hi again"); conn.commit()
    assert a == b
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events"); assert cur.fetchone()[0] == 1


def test_message_creates_conversation_and_status_received(conn, cfg):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="m1", text="hi"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, conversation_id, kind FROM events WHERE id=%s", (eid,))
        status, conv, kind = cur.fetchone()
    assert status == "received" and conv is not None and kind == "message"


def test_signal_ingest_sets_fingerprint(conn, cfg):
    eid = events.ingest_signal(conn, cfg, team="dev", source="sentry",
                               fingerprint="ISSUE-1", payload={"e": 1}); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT kind, dedup_key FROM events WHERE id=%s", (eid,))
        kind, dedup = cur.fetchone()
    assert kind == "signal" and dedup == "ISSUE-1"


def test_media_write_is_atomic(conn, cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path))
    src = tmp_path / "voice.ogg"; src.write_bytes(b"audio-bytes")
    eid = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                                text="", media=[{"kind": "audio", "src": str(src),
                                                 "mime": "audio/ogg"}])
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT path, bytes, checksum FROM media WHERE event_id=%s", (eid,))
        path, nbytes, checksum = cur.fetchone()
    assert Path(path).exists() and nbytes == len(b"audio-bytes") and checksum
