"""Token-budget fitting for assembled context: fixed degrade order (knowledge,
then oldest summaries, then oldest messages), never below the recent-message
floor, and a no-op when already under budget."""
from __future__ import annotations

from datetime import date, datetime, timezone

from argus.v2.context import budget as ctx_budget
from argus.v2.context.assemble import ContextBundle, assemble
from argus.v2.ingress import events


def _bundle(n_messages=10, n_summaries=3, n_knowledge=2):
    messages = [(i, f"message number {i} with some padding text") for i in range(n_messages)]
    summaries = [(date(2026, 1, i + 1), f"summary {i} with some padding text") for i in range(n_summaries)]
    knowledge = [{"title": f"k{i}", "content": f"knowledge content {i} padding"} for i in range(n_knowledge)]
    return ContextBundle(recent_messages=messages, summaries=summaries, knowledge=knowledge)


def test_fit_to_budget_is_noop_when_under_budget():
    bundle = _bundle()
    before = bundle.as_prompt()
    result = ctx_budget.fit_to_budget(bundle, budget_tokens=10_000)
    assert result.as_prompt() == before
    assert len(result.knowledge) == 2
    assert len(result.summaries) == 3
    assert len(result.recent_messages) == 10


def test_fit_to_budget_drops_knowledge_first():
    bundle = _bundle(n_messages=5, n_summaries=1, n_knowledge=5)
    # budget tight enough to force some trimming but generous enough that
    # dropping only knowledge should be enough to get under budget.
    tokens_without_knowledge = ctx_budget.estimate_tokens(
        ContextBundle(recent_messages=bundle.recent_messages, summaries=bundle.summaries).as_prompt()
    )
    ctx_budget.fit_to_budget(bundle, budget_tokens=tokens_without_knowledge + 5)
    assert bundle.knowledge == []
    assert len(bundle.summaries) == 1  # untouched
    assert len(bundle.recent_messages) == 5  # untouched


def test_fit_to_budget_drops_oldest_summaries_after_knowledge():
    bundle = _bundle(n_messages=5, n_summaries=4, n_knowledge=3)
    # Extremely tight budget: knowledge goes first, then summaries should be
    # trimmed oldest-first (summaries are stored oldest-first per assemble()).
    ctx_budget.fit_to_budget(bundle, budget_tokens=20)
    assert bundle.knowledge == []
    # whatever summaries remain must be a suffix (the newest ones)
    remaining_days = [d for d, _ in bundle.summaries]
    original_days = [d for d, _ in _bundle(n_messages=5, n_summaries=4, n_knowledge=3).summaries]
    assert remaining_days == original_days[len(original_days) - len(remaining_days):]


def test_fit_to_budget_never_drops_below_floor():
    bundle = _bundle(n_messages=20, n_summaries=5, n_knowledge=5)
    ctx_budget.fit_to_budget(bundle, budget_tokens=1, min_recent_messages=5)
    assert bundle.knowledge == []
    assert bundle.summaries == []
    assert len(bundle.recent_messages) == 5
    # the floor keeps the most recent messages (highest indices, since
    # recent_messages is oldest-first)
    kept = [i for i, _ in bundle.recent_messages]
    assert kept == [15, 16, 17, 18, 19]


def test_fit_to_budget_respects_custom_floor():
    bundle = _bundle(n_messages=20, n_summaries=0, n_knowledge=0)
    ctx_budget.fit_to_budget(bundle, budget_tokens=1, min_recent_messages=8)
    assert len(bundle.recent_messages) == 8


def test_assemble_end_to_end_drops_knowledge_before_messages(conn, cfg):
    from argus.v2.knowledge import store

    store.add(conn, cfg, scope="company", team_id=None, title="deploys",
              content="production deploys go through staging first, always, no exceptions here")
    conn.commit()
    for i in range(20):
        events.ingest_message(conn, cfg, team="dev", source="cli",
                              dedup_key=f"m{i}", text=f"message {i} with some extra padding text")
        conn.commit()

    bundle = assemble(conn, team_id="dev", conversation_id=None,
                      now=datetime.now(timezone.utc), cfg=cfg,
                      query="how do deploys work", token_budget=60)

    assert bundle.knowledge == []  # knowledge dropped first under a tiny budget
    assert len(bundle.recent_messages) >= 5  # floor respected


def test_assemble_default_budget_is_noop_for_small_context(conn, cfg):
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1", text="hello team")
    conn.commit()
    bundle = assemble(conn, team_id="dev", conversation_id=None,
                      now=datetime.now(timezone.utc))
    assert len(bundle.recent_messages) == 1


def test_assemble_invalid_env_budget_falls_back(monkeypatch, conn, cfg):
    monkeypatch.setenv("ARGUS_CONTEXT_TOKEN_BUDGET", "not-a-number")
    events.ingest_message(conn, cfg, team="dev", source="cli", dedup_key="m1", text="hello team")
    conn.commit()
    bundle = assemble(conn, team_id="dev", conversation_id=None,
                      now=datetime.now(timezone.utc))
    assert len(bundle.recent_messages) == 1
