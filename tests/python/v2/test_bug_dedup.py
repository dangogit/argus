"""Duplicate-work guard: the same bug report id must not open a second
pipeline dispatch anywhere in the company while one is still in flight.
Real incident (2026-07-01): bug 284b2295-9309-410f-b167-8fc3a16736d6 opened
tadam PR #324 and tadam-agents PR #302, duplicate work + owner confusion.
"""
import pytest

from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import bug_dedup, pipeline

BUG_UUID = "284b2295-9309-410f-b167-8fc3a16736d6"
BUG_PREFIX = "284b2295"


# --- extraction regex: positive and negative cases ---

@pytest.mark.parametrize("text,expected", [
    ("Argus: Investigate bug report 284b2295-9309-410f-b167-8fc3a16736d6", BUG_PREFIX),
    ("bug report 284b2295-9309-410f-b167-8fc3a16736d6 uploaded im...", BUG_PREFIX),
    ("bug 284b2295 uploaded image broken", BUG_PREFIX),
    ("this bug #284b2295 needs fixing", BUG_PREFIX),
    ("bug: 284b2295-9309-410f-b167-8fc3a16736d6", BUG_PREFIX),
])
def test_extract_bug_ref_from_text_positive(text, expected):
    assert bug_dedup.extract_bug_ref(text=text) == expected


@pytest.mark.parametrize("text", [
    "please fix the login page",
    "bug in the code somewhere",
    "database migration bugfix needed",
    "",
    None,
    "fix the checkout flow",
])
def test_extract_bug_ref_from_text_negative(text):
    assert bug_dedup.extract_bug_ref(text=text) is None


def test_extract_bug_ref_from_signal_payload_row_id():
    payload = {"message": "x", "row": {"id": BUG_UUID}}
    assert bug_dedup.extract_bug_ref(payload=payload) == BUG_PREFIX


def test_extract_bug_ref_from_signal_payload_non_uuid_row_id_is_ignored():
    payload = {"message": "x", "row": {"id": "not-a-uuid"}}
    assert bug_dedup.extract_bug_ref(payload=payload) is None


def test_extract_bug_ref_full_uuid_and_prefix_normalize_the_same():
    assert (bug_dedup.extract_bug_ref(payload={"row": {"id": BUG_UUID}})
            == bug_dedup.extract_bug_ref(text=f"bug {BUG_PREFIX}"))


def test_extract_bug_ref_payload_wins_over_text_when_both_present():
    other_uuid = "11112222-3333-4444-5555-666677778888"
    payload = {"row": {"id": BUG_UUID}}
    assert bug_dedup.extract_bug_ref(payload=payload,
                                     text=f"bug report {other_uuid}") == BUG_PREFIX


# --- config: two-team fixture ---

def _cfg_clean(tmp_path, *, company_dedup=False, dev_dedup=None, other_dedup=None):
    def _line(flag):
        if flag is None:
            return ""
        return f"    dedup_bug_dispatch: {str(flag).lower()}\n"
    y = tmp_path / "c2.yaml"
    y.write_text(
        "company:\n  name: c\n"
        f"  defaults: {{ engine: {{ engine: echo }}, dedup_bug_dispatch: {str(company_dedup).lower()} }}\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
        f"{_line(dev_dedup)}"
        "  - name: other\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
        f"{_line(other_dedup)}"
    )
    return loader.load(y)


def test_config_default_is_off(tmp_path):
    cfg = _cfg_clean(tmp_path, company_dedup=False)
    assert bug_dedup.dedup_enabled(cfg, "dev") is False


def test_config_company_default_on(tmp_path):
    cfg = _cfg_clean(tmp_path, company_dedup=True)
    assert bug_dedup.dedup_enabled(cfg, "dev") is True
    assert bug_dedup.dedup_enabled(cfg, "other") is True


def test_config_team_override_wins_over_company(tmp_path):
    cfg = _cfg_clean(tmp_path, company_dedup=True, dev_dedup=False)
    assert bug_dedup.dedup_enabled(cfg, "dev") is False
    assert bug_dedup.dedup_enabled(cfg, "other") is True


def test_config_team_can_opt_in_when_company_off(tmp_path):
    cfg = _cfg_clean(tmp_path, company_dedup=False, dev_dedup=True)
    assert bug_dedup.dedup_enabled(cfg, "dev") is True
    assert bug_dedup.dedup_enabled(cfg, "other") is False


# --- end-to-end dispatch guard ---

def _open_with_bug(conn, cfg, *, team, dedup_key, bug_uuid=BUG_UUID, source="sb-bugs"):
    eid = events.ingest_signal(conn, cfg, team=team, source=source,
                               fingerprint=dedup_key,
                               payload={"message": "uploaded im...", "row": {"id": bug_uuid}})
    return pipeline.open_request(conn, cfg, event_id=eid, team_id=team,
                                 conversation_id=None, fingerprint=dedup_key)


def test_duplicate_blocked_across_teams(tmp_path, conn):
    cfg = _cfg_clean(tmp_path, company_dedup=True)
    first = _open_with_bug(conn, cfg, team="dev", dedup_key="d1"); conn.commit()
    assert first is not None
    second = _open_with_bug(conn, cfg, team="other", dedup_key="d2"); conn.commit()
    assert second is None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE bug_ref IS NOT NULL")
        assert cur.fetchone()[0] == 1


def test_duplicate_blocked_same_team(tmp_path, conn):
    cfg = _cfg_clean(tmp_path, company_dedup=True)
    first = _open_with_bug(conn, cfg, team="dev", dedup_key="s1"); conn.commit()
    assert first is not None
    second = _open_with_bug(conn, cfg, team="dev", dedup_key="s2"); conn.commit()
    assert second is None


def test_duplicate_skip_emits_notice(tmp_path, conn):
    cfg = _cfg_clean(tmp_path, company_dedup=True)
    first = _open_with_bug(conn, cfg, team="dev", dedup_key="n1"); conn.commit()
    _open_with_bug(conn, cfg, team="other", dedup_key="n2"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT message FROM alerts WHERE fingerprint LIKE %s",
                    (f"dup-bug-dispatch:{BUG_PREFIX}:%",))
        row = cur.fetchone()
    assert row is not None
    assert "skipped duplicate" in row[0]
    assert BUG_PREFIX in row[0]
    assert "team dev" in row[0]
    assert first in row[0]


def test_terminal_request_does_not_block_new_dispatch(tmp_path, conn):
    cfg = _cfg_clean(tmp_path, company_dedup=True)
    first = _open_with_bug(conn, cfg, team="dev", dedup_key="t1"); conn.commit()
    assert first is not None
    with conn.cursor() as cur:
        cur.execute("UPDATE requests SET status='done' WHERE id=%s", (first,))
    conn.commit()
    second = _open_with_bug(conn, cfg, team="other", dedup_key="t2"); conn.commit()
    assert second is not None
    assert second != first


def test_flag_off_means_no_change(tmp_path, conn):
    cfg = _cfg_clean(tmp_path, company_dedup=False)
    first = _open_with_bug(conn, cfg, team="dev", dedup_key="f1"); conn.commit()
    assert first is not None
    second = _open_with_bug(conn, cfg, team="other", dedup_key="f2"); conn.commit()
    # Flag off: duplicate bug id does NOT block a second dispatch (current
    # behavior preserved), so a distinct request opens in the other team.
    assert second is not None
    assert second != first


def test_bug_ref_stored_on_request_row(tmp_path, conn):
    cfg = _cfg_clean(tmp_path, company_dedup=True)
    rid = _open_with_bug(conn, cfg, team="dev", dedup_key="r1"); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT bug_ref FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == BUG_PREFIX


def test_no_bug_ref_never_blocks(tmp_path, conn):
    cfg = _cfg_clean(tmp_path, company_dedup=True)
    eid1 = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1",
                                 text="fix the login page styling")
    r1 = pipeline.open_request(conn, cfg, event_id=eid1, team_id="dev", conversation_id=None)
    conn.commit()
    eid2 = events.ingest_message(conn, cfg, team="other", source="cli", dedup_key="m2",
                                 text="fix the login page styling")
    r2 = pipeline.open_request(conn, cfg, event_id=eid2, team_id="other", conversation_id=None)
    conn.commit()
    assert r1 is not None and r2 is not None and r1 != r2


def test_find_duplicate_returns_none_when_no_match(conn):
    assert bug_dedup.find_duplicate(conn, "deadbeef") is None
