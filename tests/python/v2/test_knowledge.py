from argus.v2.knowledge import store


def test_scope_isolation(conn, cfg):
    store.add(conn, cfg, scope="company", team_id=None, title="c", content="company wide policy")
    store.add(conn, cfg, scope="team", team_id="dev", title="d", content="dev team secret deploy")
    store.add(conn, cfg, scope="team", team_id="mkt", title="m", content="marketing brand voice")
    conn.commit()
    dev = {r["title"] for r in store.search(conn, cfg, team_id="dev", query="policy deploy", k=10)}
    assert "c" in dev and "d" in dev and "m" not in dev   # dev sees company + dev, not mkt


def test_semantic_ranks_closest_first(conn, cfg):
    store.add(conn, cfg, scope="company", team_id=None, title="db", content="postgres database tuning indexes")
    store.add(conn, cfg, scope="company", team_id=None, title="ui", content="frontend react components styling")
    conn.commit()
    top = store.search(conn, cfg, team_id="dev", query="database index performance", k=1)
    assert top and top[0]["title"] == "db"


def test_keyword_fallback_when_no_embedding(conn, cfg):
    # Insert with a null embedding directly; keyword search still finds it.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO knowledge (scope, team_id, title, content) "
                    "VALUES ('company', NULL, 'k', 'unique_marker_xyz here')")
    conn.commit()
    res = store.search(conn, cfg, team_id="dev", query="unique_marker_xyz", k=5)
    assert any(r["title"] == "k" for r in res)
