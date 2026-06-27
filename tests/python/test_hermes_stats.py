# tests/python/test_hermes_stats.py
import sqlite3

from argus.hermes.stats import last_session_cost


def _make_db(home, rows):
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / "state.db")
    db.execute(
        "CREATE TABLE sessions (id TEXT, source TEXT, started_at TEXT, estimated_cost_usd REAL)"
    )
    db.executemany("INSERT INTO sessions VALUES (?,?,?,?)", rows)
    db.commit()
    db.close()


def test_latest_cli_session_cost(tmp_path):
    _make_db(
        tmp_path / "prof",
        [
            ("a", "cli", "2026-06-12T01:00:00", 0.0123),
            ("b", "gateway", "2026-06-12T02:00:00", 9.9),
            ("c", "cli", "2026-06-12T03:00:00", 0.0456),
        ],
    )
    assert last_session_cost(tmp_path / "prof") == "0.0456"


def test_missing_db_returns_none(tmp_path):
    assert last_session_cost(tmp_path / "nope") is None


def test_corrupt_db_fails_open(tmp_path):
    home = tmp_path / "bad"
    home.mkdir()
    (home / "state.db").write_text("not a database")
    assert last_session_cost(home) is None


def test_null_cost_returns_none(tmp_path):
    _make_db(tmp_path / "p2", [("a", "cli", "2026-06-12T01:00:00", None)])
    assert last_session_cost(tmp_path / "p2") is None
