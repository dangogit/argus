import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from psycopg.types.json import Json

from argus.v2.ingress import events
from argus.v2.memory import brief as project_memory

REGRESSION_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "project_memory_regressions.json").read_text(
        encoding="utf-8"
    )
)


def _request(conn, cfg, *, team, key, text, status="open"):
    event_id = events.ingest_message(
        conn, cfg, team=team, source="cli", dedup_key=key, text=text
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO requests (event_id, team_id, status) VALUES (%s,%s,%s) RETURNING id",
            (event_id, team, status),
        )
        return str(cur.fetchone()[0])


def _summary(conn, *, team, marker, details):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO conversation_summaries
               (team_id, day, summary, message_count, details, source_fingerprint)
               VALUES (%s, CURRENT_DATE, %s, 1, %s, %s)""",
            (team, marker, Json(details), marker),
        )


def test_build_is_team_scoped_and_keeps_company_knowledge(conn, cfg):
    dev_request = _request(conn, cfg, team="dev", key="brief-dev", text="fix dev login")
    _request(conn, cfg, team="other", key="brief-other", text="OTHER SECRET WORK")
    _summary(
        conn,
        team="dev",
        marker="dev-summary",
        details={
            "status": "semantic",
            "decisions": [{"text": "DEV DECISION", "evidence_ids": []}],
            "open_loops": [{"text": "DEV LOOP", "evidence_ids": []}],
            "outcomes": [{"text": "DEV OUTCOME", "evidence_ids": []}],
        },
    )
    _summary(
        conn,
        team="other",
        marker="other-summary",
        details={
            "status": "semantic",
            "decisions": [{"text": "OTHER DECISION", "evidence_ids": []}],
            "open_loops": [{"text": "OTHER LOOP", "evidence_ids": []}],
            "outcomes": [{"text": "OTHER OUTCOME", "evidence_ids": []}],
        },
    )
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO pm_lessons (team_id, fingerprint, finding, outcome)
               VALUES ('dev','dev-lesson','DEV LESSON','qa-pass'),
                      ('other','other-lesson','OTHER LESSON','qa-pass')"""
        )
        cur.execute(
            """INSERT INTO knowledge (scope, team_id, title, content, source)
               VALUES ('team','dev','dev','DEV KNOWLEDGE','test'),
                      ('team','other','other','OTHER KNOWLEDGE','test'),
                      ('company',NULL,'company','COMPANY KNOWLEDGE','test')"""
        )
        cur.execute(
            """INSERT INTO retro_backlog
               (id, team_id, type, status, statement)
               VALUES ('dev-retro','dev','skill','gated','DEV RETRO'),
                      ('other-retro','other','skill','gated','OTHER RETRO')"""
        )
        cur.execute(
            """INSERT INTO actions
               (team_id, type, risk, status, idempotency_key, provider_ref)
               VALUES ('dev','open_pr','reversible_internal','done','brief-dev-pr',
                       'https://github.com/acme/dev/pull/1'),
                      ('other','open_pr','reversible_internal','done','brief-other-pr',
                       'https://github.com/acme/other/pull/2')"""
        )
    conn.commit()

    brief = project_memory.build(
        conn, cfg, "dev", datetime.now(timezone.utc)
    )
    text = project_memory.render_text(brief)

    assert dev_request in text
    for expected in (
        "DEV LOOP",
        "DEV DECISION",
        "DEV LESSON",
        "DEV KNOWLEDGE",
        "COMPANY KNOWLEDGE",
        "DEV OUTCOME",
        "https://github.com/acme/dev/pull/1",
        "DEV RETRO",
    ):
        assert expected in text
    for forbidden in (
        "OTHER SECRET WORK",
        "OTHER LOOP",
        "OTHER DECISION",
        "OTHER LESSON",
        "OTHER KNOWLEDGE",
        "OTHER OUTCOME",
        "https://github.com/acme/other/pull/2",
        "OTHER RETRO",
    ):
        assert forbidden not in text


def test_build_redacts_secrets_from_raw_and_legacy_memory(conn, cfg):
    _request(
        conn,
        cfg,
        team="dev",
        key="brief-secret",
        text="rotate token Bearer do-not-print-me",
    )
    _summary(
        conn,
        team="dev",
        marker="secret-summary",
        details={
            "status": "semantic",
            "decisions": [
                {"text": "password=hunter2 was replaced", "evidence_ids": []}
            ],
            "open_loops": [],
            "outcomes": [],
        },
    )
    conn.commit()

    brief = project_memory.build(conn, cfg, "dev", datetime.now(timezone.utc))
    rendered = project_memory.render_text(brief)

    assert "do-not-print-me" not in rendered
    assert "hunter2" not in rendered
    assert "[REDACTED" in rendered


def test_renderers_are_deterministic_and_prompt_preserves_mandatory_sections():
    item = project_memory.BriefItem
    brief = project_memory.ProjectMemoryBrief(
        team_id="dev",
        generated_at="2026-07-14T00:00:00+00:00",
        current_work=tuple(item(f"CURRENT {index} " + "c" * 500) for index in range(8)),
        open_loops=tuple(item(f"LOOP {index} " + "l" * 500) for index in range(8)),
        recent_decisions=(item("LATEST DECISION " + "d" * 500), item("OLD DECISION")),
        validated_lessons=tuple(item("LESSON " + "x" * 500) for _ in range(5)),
        recent_outcomes=tuple(item("OUTCOME " + "y" * 500) for _ in range(5)),
        pending_retro=tuple(item("RETRO " + "z" * 500) for _ in range(5)),
    )

    prompt = project_memory.render_prompt(brief)
    payload = json.loads(project_memory.render_json(brief))

    assert len(prompt) <= 3_000
    assert "Current work and approval waits" in prompt
    assert "Open loops" in prompt
    assert "LATEST DECISION" in prompt
    assert payload["team_id"] == "dev"
    assert payload["recent_decisions"][0]["text"].startswith("LATEST DECISION")
    assert project_memory.render_json(brief) == project_memory.render_json(brief)


@pytest.mark.parametrize("case", REGRESSION_FIXTURES, ids=lambda case: case["id"])
def test_project_memory_regression_fixture(conn, cfg, case):
    _seed_regression_case(conn, cfg, case)
    conn.commit()

    brief = project_memory.build(conn, cfg, "dev", datetime.now(timezone.utc))
    rendered = project_memory.render_prompt(brief)

    for expected in case["expected"]:
        assert expected in rendered
    for forbidden in case["forbidden"]:
        assert forbidden not in rendered


def test_regression_fixture_inventory_is_exact():
    groups = {}
    for case in REGRESSION_FIXTURES:
        groups[case["group"]] = groups.get(case["group"], 0) + 1

    assert groups == {"recall": 8, "no_recall": 4, "isolation": 4, "safety": 4}


def _seed_regression_case(conn, cfg, case):
    setup = case["setup"]
    team = case.get("team")
    text = case.get("text", "")
    if setup == "request":
        _request(
            conn,
            cfg,
            team=team,
            key=case["id"],
            text=text,
            status=case["status"],
        )
        return
    if setup in ("summary", "invalid_evidence"):
        field = case.get("field", "decisions")
        evidence = (
            ["event:00000000-0000-0000-0000-000000000000"]
            if setup == "invalid_evidence"
            else []
        )
        details = {
            "status": "semantic",
            "decisions": [],
            "open_loops": [],
            "outcomes": [],
        }
        details[field] = [{"text": text, "evidence_ids": evidence}]
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO conversation_summaries
                   (team_id, day, summary, message_count, details, source_fingerprint)
                   VALUES (%s, CURRENT_DATE + %s, %s, 1, %s, %s)""",
                (team, case.get("day_offset", 0), case["id"], Json(details), case["id"]),
            )
        return
    if setup == "lesson":
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pm_lessons (team_id, fingerprint, finding, outcome)
                   VALUES (%s,%s,%s,%s)""",
                (team, case["id"], text, case["outcome"]),
            )
        return
    if setup == "knowledge":
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO knowledge (scope, team_id, title, content, source)
                   VALUES (%s,%s,%s,%s,'fixture')""",
                (case["scope"], team, case["id"], text),
            )
        return
    if setup == "outcome_pr":
        _summary(
            conn,
            team=team,
            marker=case["id"],
            details={
                "status": "semantic",
                "decisions": [],
                "open_loops": [],
                "outcomes": [{"text": text, "evidence_ids": []}],
            },
        )
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO actions
                   (team_id, type, risk, status, idempotency_key, provider_ref)
                   VALUES (%s,'open_pr','reversible_internal','done',%s,%s)""",
                (team, case["id"], case["url"]),
            )
        return
    if setup == "retro":
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO retro_backlog (id, team_id, type, status, statement)
                   VALUES (%s,%s,'skill','gated',%s)""",
                (case["id"], team, text),
            )
        return
    if setup == "action":
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO actions
                   (team_id, type, risk, status, idempotency_key, payload)
                   VALUES (%s,%s,'reversible_internal',%s,%s,%s)""",
                (team, case["action_type"], case["status"], case["id"], Json({"text": text})),
            )
        return
    raise AssertionError(f"unknown regression setup: {setup}")
