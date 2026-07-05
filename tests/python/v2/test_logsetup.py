"""JSON log formatter: standard fields present, extra= fields serialized, and
ARGUS_LOG_JSON=1 opts the configured handler into JSON. Hermetic - no DB."""
from __future__ import annotations

import json
import logging

from argus.v2 import logsetup


def _record(msg="hello %s", args=("world",), extra=None, level=logging.INFO):
    record = logging.LogRecord(
        name="argus.test", level=level, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None)
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def test_json_formatter_standard_fields():
    line = logsetup.JsonFormatter().format(_record())
    payload = json.loads(line)
    assert payload["msg"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "argus.test"
    assert payload["ts"].endswith("Z")


def test_json_formatter_serializes_extra_fields():
    line = logsetup.JsonFormatter().format(_record(extra={
        "job_id": "j1", "team_id": "dev", "request_id": None,
        "sweep": {"events_routed": 2}}))
    payload = json.loads(line)
    assert payload["job_id"] == "j1"
    assert payload["team_id"] == "dev"
    assert payload["request_id"] is None
    assert payload["sweep"] == {"events_routed": 2}


def test_json_formatter_handles_unserializable_extra():
    # default=str: an arbitrary object must not crash the formatter.
    line = logsetup.JsonFormatter().format(_record(extra={"obj": object()}))
    assert "obj" in json.loads(line)


def _isolate_root(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(root, "_argus_configured", False, raising=False)
    return root


def test_configure_plain_by_default(monkeypatch):
    monkeypatch.delenv("ARGUS_LOG_JSON", raising=False)
    root = _isolate_root(monkeypatch)
    logsetup.configure()
    assert not isinstance(root.handlers[-1].formatter, logsetup.JsonFormatter)


def test_configure_json_opt_in_and_idempotent(monkeypatch):
    monkeypatch.setenv("ARGUS_LOG_JSON", "1")
    root = _isolate_root(monkeypatch)
    logsetup.configure()
    logsetup.configure()  # second call must not add a second handler
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, logsetup.JsonFormatter)
