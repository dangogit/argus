from psycopg.types.json import Json

from argus.v2.pm import memory


def _request(conn, team="dev", fingerprint="F1", text="fix login"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO events (team_id, kind, source, payload, dedup_key)
            VALUES (%s,'signal','pm',%s,%s)
            RETURNING id
            """,
            (team, Json({"text": text}), fingerprint),
        )
        eid = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO requests (event_id, team_id, fingerprint)
            VALUES (%s,%s,%s)
            RETURNING id
            """,
            (eid, team, fingerprint),
        )
        return str(cur.fetchone()[0])


def test_render_is_empty_without_lessons(conn):
    assert memory.render(conn, team_id="dev") == ""


def test_render_dedups_latest_and_honors_limit(conn):
    memory.append(conn, team_id="dev", fingerprint="old", finding="old bug",
                  outcome="proposed")
    memory.append(conn, team_id="dev", fingerprint="dup", finding="first",
                  outcome="proposed")
    memory.append(conn, team_id="dev", fingerprint="dup", finding="second",
                  outcome="qa-pass", note="merged")

    text = memory.render(conn, team_id="dev", limit=1)

    assert "dup" in text
    assert "qa-pass" in text
    assert "second" in text
    assert "old bug" not in text
    assert "first" not in text


def test_suppressed_fingerprints_are_not_rendered(conn):
    memory.append(conn, team_id="dev", fingerprint="bad", finding="bad idea",
                  outcome="proposed")
    memory.append(conn, team_id="dev", fingerprint="good", finding="good idea",
                  outcome="qa-pass")
    for idx, outcome in enumerate(["qa-fail", "qa-fail", "qa-fail", "qa-fail", "qa-pass"]):
        memory.attribute(conn, team_id="dev", request_id=_request(conn, fingerprint=f"R{idx}"),
                         fingerprints=["bad"], outcome=outcome)

    text = memory.render(conn, team_id="dev")

    assert "good idea" in text
    assert "bad idea" not in text


def test_record_request_outcome_writes_lesson_and_attribution(conn):
    rid = _request(conn, fingerprint="new", text="fix payment")
    memory.append(conn, team_id="dev", fingerprint="prior", finding="prior lesson",
                  outcome="qa-pass")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs
              (request_id, team_id, role, stage, kind, idempotency_key, result)
            VALUES (%s,'dev','developer',0,'pipeline','mem-builder',%s)
            """,
            (rid, Json({"memory_fingerprints": ["prior", "new"]})),
        )

    memory.record_request_outcome(conn, request_id=rid, outcome="qa-pass", note="approved")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT finding, outcome, note FROM pm_lessons "
            "WHERE team_id='dev' AND fingerprint='new'"
        )
        assert cur.fetchone() == ("fix payment", "qa-pass", "approved")
        cur.execute(
            "SELECT lesson_fingerprint, outcome FROM pm_lesson_attributions "
            "WHERE request_id=%s",
            (rid,),
        )
        assert cur.fetchall() == [("prior", "qa-pass")]
