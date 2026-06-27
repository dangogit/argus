"""Single connection factory. ARGUS_DB_DSN is the one knob."""
import os

import psycopg


def dsn() -> str:
    d = os.environ.get("ARGUS_DB_DSN")
    if not d:
        raise RuntimeError("ARGUS_DB_DSN is not set")
    return d


def connect() -> psycopg.Connection:
    return psycopg.connect(dsn())  # autocommit=False (default)
