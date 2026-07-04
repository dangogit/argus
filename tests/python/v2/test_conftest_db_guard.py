"""Regression for the 2026-07-04 incident: an ambient ARGUS_DB_DSN (leaked
live DSN in a dev shell) must NOT be handed to the truncating test suite.
External DSNs are honored only under CI or ARGUS_DB_DSN_FOR_TESTS=1."""
import importlib.util
from pathlib import Path


def _load_conftest():
    spec = importlib.util.spec_from_file_location(
        "v2_conftest_under_test", Path(__file__).with_name("conftest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ambient_dsn_refused_outside_ci(monkeypatch):
    mod = _load_conftest()
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ARGUS_DB_DSN_FOR_TESTS", raising=False)
    assert not mod._external_dsn_allowed()


def test_dsn_allowed_under_ci(monkeypatch):
    mod = _load_conftest()
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("ARGUS_DB_DSN_FOR_TESTS", raising=False)
    assert mod._external_dsn_allowed()


def test_dsn_allowed_with_explicit_opt_in(monkeypatch):
    mod = _load_conftest()
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("ARGUS_DB_DSN_FOR_TESTS", "1")
    assert mod._external_dsn_allowed()
