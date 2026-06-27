"""Opt-in live smoke tests for connectors.

Run only with ARGUS_LIVE=1, never during the gate.
Each test polls a real external service read-only and asserts no exception is
raised. If the connector's required secret env var is absent, the test is
skipped so a partial live environment still passes.

Usage:
    ARGUS_LIVE=1 .venv/bin/pytest tests/python/v2/live -q

These tests are excluded from the gate via --ignore=tests/python/v2/live in
scripts/v2-gate.sh, so they never count as skipped tests under ARGUS_GATE=1.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

# All tests in this module require ARGUS_LIVE=1.
pytestmark = pytest.mark.live


def _live():
    if os.environ.get("ARGUS_LIVE") != "1":
        pytest.skip("set ARGUS_LIVE=1 to run live smoke tests")


def _source(type_: str, name: str, secret: str | None, config: dict):
    """Build a minimal SourceRef-like object from plain values."""
    return SimpleNamespace(type=type_, name=name, secret=secret, config=config)


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_sentry_poll():
    """Poll Sentry issues read-only; returns a (signals, state) pair."""
    _live()
    token = os.environ.get("SENTRY_AUTH_TOKEN") or os.environ.get("SENTRY_TOKEN")
    org = os.environ.get("SENTRY_ORG")
    project = os.environ.get("SENTRY_PROJECT")
    if not (token and org and project):
        pytest.skip("SENTRY_AUTH_TOKEN (or SENTRY_TOKEN), SENTRY_ORG, and SENTRY_PROJECT required")

    from argus.v2.connectors.sentry import SentryConnector
    cfg = {"org": org, "project": project}
    base_url = os.environ.get("SENTRY_BASE_URL")
    if base_url:
        cfg["base_url"] = base_url

    source = _source("sentry", "live-sentry", token, cfg)
    conn = SentryConnector()
    signals, state = conn.poll(source, {})
    assert isinstance(signals, list)
    assert isinstance(state, dict)
    assert "last_seen" in state


# ---------------------------------------------------------------------------
# Vercel
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_vercel_poll():
    """Poll Vercel deployments read-only; returns a (signals, state) pair."""
    _live()
    token = os.environ.get("VERCEL_TOKEN")
    if not token:
        pytest.skip("VERCEL_TOKEN required")

    from argus.v2.connectors.vercel import VercelConnector
    cfg: dict = {}
    project = os.environ.get("VERCEL_PROJECT")
    team = os.environ.get("VERCEL_TEAM")
    if project:
        cfg["project"] = project
    if team:
        cfg["team"] = team

    source = _source("vercel", "live-vercel", token, cfg)
    conn = VercelConnector()
    signals, state = conn.poll(source, {})
    assert isinstance(signals, list)
    assert isinstance(state, dict)
    assert "last_created" in state


# ---------------------------------------------------------------------------
# PostHog
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_posthog_poll():
    """Poll PostHog activity log read-only; returns a (signals, state) pair."""
    _live()
    token = os.environ.get("POSTHOG_TOKEN") or os.environ.get("POSTHOG_PERSONAL_API_KEY")
    project_id = os.environ.get("POSTHOG_PROJECT_ID")
    if not (token and project_id):
        pytest.skip("POSTHOG_TOKEN (or POSTHOG_PERSONAL_API_KEY) and POSTHOG_PROJECT_ID required")

    from argus.v2.connectors.posthog import PostHogConnector
    cfg: dict = {"project": project_id}
    host = os.environ.get("POSTHOG_HOST")
    if host:
        cfg["host"] = host

    source = _source("posthog", "live-posthog", token, cfg)
    conn = PostHogConnector()
    signals, state = conn.poll(source, {})
    assert isinstance(signals, list)
    assert isinstance(state, dict)
    assert "last_created" in state


# ---------------------------------------------------------------------------
# Postgres (live prod DB, read-only query)
# ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_postgres_poll():
    """Poll a live Postgres table read-only; returns a (signals, state) pair."""
    _live()
    dsn = os.environ.get("ARGUS_LIVE_PG_DSN")
    table = os.environ.get("ARGUS_LIVE_PG_TABLE")
    cursor_col = os.environ.get("ARGUS_LIVE_PG_CURSOR_COL")
    if not (dsn and table and cursor_col):
        pytest.skip(
            "ARGUS_LIVE_PG_DSN, ARGUS_LIVE_PG_TABLE, and ARGUS_LIVE_PG_CURSOR_COL required"
        )

    from argus.v2.connectors.postgres import PostgresConnector
    cfg: dict = {
        "dsn": dsn,
        "table": table,
        "cursor_column": cursor_col,
        "page_size": 1,  # minimal read
    }
    cursor_type = os.environ.get("ARGUS_LIVE_PG_CURSOR_TYPE", "int")
    cfg["cursor_type"] = cursor_type

    source = _source("postgres", "live-postgres", None, cfg)
    conn = PostgresConnector()
    signals, state = conn.poll(source, {})
    assert isinstance(signals, list)
    assert isinstance(state, dict)
    assert "last" in state
