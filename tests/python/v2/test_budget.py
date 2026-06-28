"""Global daily cost ceiling: sum recent priced runs, pause new work over cap."""
from __future__ import annotations

from pathlib import Path

from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import budget, reconcile
from argus.v2.queue import jobs

_counter = iter(range(10_000))


def _cfg_with_cap(tmp_path, cap):
    y = tmp_path / "c.yaml"
    cap_line = f", max_daily_cost_usd: {cap}" if cap is not None else ""
    y.write_text(
        "company:\n  name: c\n"
        f"  defaults: {{ engine: {{ engine: echo }}{cap_line} }}\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n")
    return loader.load(y)


def _cfg_team_cap(tmp_path, cap):
    y = tmp_path / "tc.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        f"    pipeline: {{ stages: [developer] }}\n    max_daily_cost_usd: {cap}\n")
    return loader.load(y)


def _run(conn, cost, *, hours_ago=0, team="dev"):
    job_id = jobs.enqueue(conn, team_id=team, kind="pipeline", role="developer",
                          stage=0, idempotency_key=f"k{next(_counter)}",
                          exec_snapshot={}, payload={})
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runs (job_id, attempt, claim_token, role, engine, cost_usd, status, ended_at) "
            "VALUES (%s,1,gen_random_uuid(),'developer','echo',%s,'ok', now() - make_interval(hours => %s))",
            (job_id, cost, hours_ago))
    conn.commit()


def test_daily_cost_sums_recent_numeric_only(conn):
    _run(conn, "0.50")
    _run(conn, "1.50")
    _run(conn, "")          # unpriced blank: ignored
    _run(conn, "99.00", hours_ago=30)  # outside the 24h window: ignored
    assert budget.daily_cost_usd(conn) == 2.00


def test_no_cap_means_never_over_budget(conn, tmp_path):
    cfg = _cfg_with_cap(tmp_path, None)
    _run(conn, "1000.00")
    assert budget.ceiling(cfg) is None
    assert budget.over_budget(conn, cfg) is False


def test_over_budget_when_spend_reaches_cap(conn, tmp_path):
    cfg = _cfg_with_cap(tmp_path, 1.0)
    _run(conn, "0.50")
    assert budget.over_budget(conn, cfg) is False
    _run(conn, "0.60")  # total 1.10 >= 1.0
    assert budget.over_budget(conn, cfg) is True


def test_route_events_pauses_when_over_budget(conn, tmp_path, monkeypatch):
    cfg = _cfg_with_cap(tmp_path, 1.0)
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="m1", text="hello")
    conn.commit()
    monkeypatch.setattr(budget, "over_budget", lambda c, cf, team_id=None: True)
    assert reconcile.route_events(conn, cfg) == 0
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM events WHERE id=%s", (eid,))
        assert cur.fetchone()[0] != "processed"  # held, not consumed


# --- per-team budgets ---

def test_daily_cost_scoped_to_team(conn):
    _run(conn, "1.00", team="dev")
    _run(conn, "5.00", team="other")
    assert budget.daily_cost_usd(conn, "dev") == 1.00
    assert budget.daily_cost_usd(conn) == 6.00  # company-wide


def test_team_over_its_cap_when_company_uncapped(conn, tmp_path):
    cfg = _cfg_team_cap(tmp_path, 1.0)  # team dev cap 1.0, no company cap
    _run(conn, "1.50", team="dev")
    assert budget.over_budget(conn, cfg, "dev") is True
    assert budget.over_budget(conn, cfg) is False  # company-wide: no cap


def test_route_events_defers_over_budget_team(conn, tmp_path):
    cfg = _cfg_team_cap(tmp_path, 0.01)  # tiny team cap
    _run(conn, "1.00", team="dev")       # dev is over its cap
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="m2", text="hi")
    conn.commit()
    reconcile.route_events(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM events WHERE id=%s", (eid,))
        assert cur.fetchone()[0] == "received"  # deferred for retry, not stranded


def test_daily_cost_ignores_bad_data(conn):
    _run(conn, "1.00")
    for bad in ("unpriced", "1.2e3", "NaN", "Infinity", " 2 ", "10;DROP"):
        _run(conn, bad)  # non-decimal: regex must reject, never reach ::numeric
    assert budget.daily_cost_usd(conn) == 1.00


def _cfg_two_teams(tmp_path, dev_cap):
    y = tmp_path / "two.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        f"    pipeline: {{ stages: [developer] }}\n    max_daily_cost_usd: {dev_cap}\n"
        "  - name: ops\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n")
    return loader.load(y)


def test_route_defers_over_team_processes_under_team(conn, tmp_path):
    cfg = _cfg_two_teams(tmp_path, 0.01)
    _run(conn, "1.00", team="dev")  # dev over its tiny cap; ops uncapped
    e_dev = events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="d1", text="hi")
    e_ops = events.ingest_message(conn, cfg, team="ops", source="cli", dedup_key="o1", text="hi")
    conn.commit()
    reconcile.route_events(conn, cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT status, defer_until FROM events WHERE id=%s", (e_dev,))
        status, defer_until = cur.fetchone()
        assert status == "received" and defer_until is not None  # deferred with backoff
        cur.execute("SELECT status FROM events WHERE id=%s", (e_ops,))
        assert cur.fetchone()[0] == "processed"  # under-budget team still flows
