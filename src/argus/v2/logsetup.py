"""Logging setup for the long-running v2 entrypoints (`argus up`, `argus serve`).

Plain text by default (unchanged behavior). Set ARGUS_LOG_JSON=1 to switch the
root handler to one JSON object per line, so "what happened overnight" can be
grepped/jq'd instead of parsed out of free text. No new dependencies.
"""
from __future__ import annotations

import json
import logging
import os
import time

# Attributes every LogRecord carries; anything else on the record came from
# logging.info(..., extra={...}) and belongs in the JSON output.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class JsonFormatter(logging.Formatter):
    """One JSON object per record: ts, level, logger, msg, plus any extra= fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: int = logging.INFO) -> None:
    """Configure the root logger once for a long-running entrypoint. Plain text
    unless ARGUS_LOG_JSON=1. Safe to call more than once (idempotent)."""
    root = logging.getLogger()
    if getattr(root, "_argus_configured", False):
        return
    handler = logging.StreamHandler()
    if os.environ.get("ARGUS_LOG_JSON") == "1":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    root._argus_configured = True  # guard against double-configure (idempotent)
