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


def test_environment_blocker_rendering_is_pure():
    pg_lesson = memory.Lesson(
        "pg", "change otherwise passed", "qa-fail",
        "Postgres check blocked by sandbox networking",
    )
    pypi_lesson = memory.Lesson(
        "pkg", "package change passed", "qa-fail",
        "PyPI check could not run because network was blocked",
    )

    assert memory._render_outcome(pg_lesson) == "environment-blocker"
    assert memory._render_note(pg_lesson) == (
        "Environment blocker: Postgres check blocked by sandbox networking"
    )
    assert memory._render_outcome(pypi_lesson) == "environment-blocker"
    assert memory._render_note(pypi_lesson).startswith("Environment blocker: PyPI")


def test_environment_blocker_rendering_handles_sandbox_permission_errors():
    lesson = memory.Lesson(
        "pg",
        "change otherwise passed",
        "qa-fail",
        "Local Postgres check failed: PermissionError: Operation not permitted",
    )

    assert memory._render_outcome(lesson) == "environment-blocker"
    assert memory._render_note(lesson).startswith("Environment blocker: Local Postgres")


def test_environment_blocker_rendering_preserves_passes():
    lesson = memory.Lesson(
        "pg",
        "change otherwise passed",
        "qa-pass",
        "Postgres check blocked by sandbox networking",
    )

    assert memory._render_outcome(lesson) == "qa-pass"
    assert memory._render_note(lesson) == "Postgres check blocked by sandbox networking"


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


def test_render_labels_sandbox_check_failures_as_environment_blockers(conn):
    memory.append(conn, team_id="dev", fingerprint="pg", finding="change otherwise passed",
                  outcome="qa-fail",
                  note="Postgres check blocked by sandbox networking")
    memory.append(conn, team_id="dev", fingerprint="pkg", finding="package change passed",
                  outcome="qa-fail",
                  note="PyPI check could not run because network was blocked")

    text = memory.render(conn, team_id="dev")

    assert "environment-blocker: change otherwise passed" in text
    assert "environment-blocker: package change passed" in text
    assert "Environment blocker: Postgres check blocked by sandbox networking" in text
    assert "Environment blocker: PyPI check could not run" in text
    assert "qa-fail: change otherwise passed" not in text
    assert "qa-fail: package change passed" not in text


def test_environment_blocker_does_not_override_later_pass(conn):
    memory.append(conn, team_id="dev", fingerprint="pg", finding="change",
                  outcome="qa-fail",
                  note="Postgres check blocked by sandbox networking")
    memory.append(conn, team_id="dev", fingerprint="pg", finding="change",
                  outcome="qa-pass", note="approved")

    text = memory.render(conn, team_id="dev")

    assert "qa-pass: change" in text
    assert "environment-blocker: change" not in text


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


def test_record_request_outcome_stores_sandbox_checks_as_blocked(conn):
    rid = _request(conn, fingerprint="pg", text="fix package check")

    memory.record_request_outcome(
        conn,
        request_id=rid,
        outcome="qa-fail",
        note="PyPI check blocked by sandbox networking",
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, note FROM pm_lessons "
            "WHERE team_id='dev' AND fingerprint='pg'"
        )
        assert cur.fetchone() == (
            "blocked",
            "Environment blocker: PyPI check blocked by sandbox networking",
        )
