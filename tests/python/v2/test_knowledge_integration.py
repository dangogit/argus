from datetime import datetime, timezone

from argus.v2.context import assemble as ctx
from argus.v2.knowledge import store
from argus.v2.actions import executor


def test_assembled_context_includes_relevant_knowledge(conn, cfg):
    store.add(conn, cfg, scope="company", team_id=None, title="deploys",
              content="production deploys go through staging first")
    conn.commit()
    bundle = ctx.assemble(conn, team_id="dev", conversation_id=None,
                          now=datetime.now(timezone.utc), cfg=cfg, query="how do deploys work")
    assert "staging first" in bundle.as_prompt()


def test_owner_rules_render_company_and_team(conn, cfg):
    from argus.v2.rules import context as rules_context

    store.add(conn, cfg, scope="company", team_id=None, title="company",
              content="All workers obey global quiet hours.", source="owner-rule")
    store.add(conn, cfg, scope="team", team_id="dev", title="team",
              content="Dev team reads AGENTS.md before edits.", source="support-rule")
    conn.commit()

    block = rules_context.block_for(conn, cfg, team_id="dev")

    assert "OWNER RULES" in block
    assert "global quiet hours" in block
    assert "AGENTS.md" in block


def test_search_sources_filter_preserves_default(conn, cfg):
    store.add(conn, cfg, scope="team", team_id="dev", title="guidance",
              content="cache TTL is 60s", source="support-guidance")
    store.add(conn, cfg, scope="team", team_id="dev", title="rule",
              content="cache TTL is 90s", source="support-rule")
    conn.commit()

    all_rows = store.search(conn, cfg, team_id="dev", query="cache TTL", k=5)
    rule_rows = store.search(conn, cfg, team_id="dev", query="cache TTL", k=5,
                             sources=["support-rule"])

    assert len(all_rows) >= 2
    assert [row["title"] for row in rule_rows] == ["rule"]


def test_remember_action_stores_knowledge(conn, cfg):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (team_id, type, risk, idempotency_key, payload) "
            "VALUES ('dev','remember','reversible_internal','r0',"
            "'{\"scope\":\"team\",\"title\":\"learned\",\"content\":\"the cache TTL is 60s\"}')")
    conn.commit()
    executor.process_proposed(conn, cfg)
    conn.commit()
    res = store.search(conn, cfg, team_id="dev", query="cache TTL", k=5)
    assert any("cache TTL" in r["content"] for r in res)
