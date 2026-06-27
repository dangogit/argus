"""Ephemeral Postgres for tests: one initdb'd cluster per session on a random
port, migrated once; every test gets a clean DB via TRUNCATE."""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "src" / "argus" / "v2" / "db" / "migrations"
# The live operational run-root. Tests must never resolve to this; the phase-D
# parity run once wrote real alert/ask fixtures into ~/argus-run/wa/ask/*.
_LIVE_RUN_ROOT = (Path.home() / "argus-run").resolve()


@pytest.fixture(autouse=True, scope="session")
def _isolate_run_root(tmp_path_factory):
    """Force ARGUS_RUN_ROOT to a throwaway dir for the whole v2 suite so no test
    can fall back to the live ~/argus-run. Resolvers default to ~/argus-run when
    the env is unset (see brief/ceo.py, actions/handlers.py, cli.py); this makes
    that default safe even if a test forgets to set its own tmp run-root.
    Per-test monkeypatch overrides still win; the _guard_run_root tripwire below
    rejects any override that points back at the live root."""
    root = tmp_path_factory.mktemp("argus-run")
    prev = os.environ.get("ARGUS_RUN_ROOT")
    os.environ["ARGUS_RUN_ROOT"] = str(root)
    yield
    if prev is None:
        os.environ.pop("ARGUS_RUN_ROOT", None)
    else:
        os.environ["ARGUS_RUN_ROOT"] = prev


@pytest.fixture(autouse=True)
def _guard_run_root(monkeypatch):
    """Fail loudly if a test points ARGUS_RUN_ROOT at the live ~/argus-run.
    ponytail: we guard the env var, not actual writes -- ~/argus-run is written
    concurrently by the live com.argus services, so file-diffing it would false-
    positive. Every run-root resolver reads this env first, so this catches the
    real leak vector. Depends on `monkeypatch` so this finalizer runs (LIFO)
    before monkeypatch restores the env, i.e. while the test's setenv is live."""
    yield
    cur = os.environ.get("ARGUS_RUN_ROOT")
    assert cur, "ARGUS_RUN_ROOT unset during test: a resolver could write to live ~/argus-run"
    resolved = Path(cur).expanduser().resolve()
    assert resolved != _LIVE_RUN_ROOT and _LIVE_RUN_ROOT not in resolved.parents, (
        f"test set ARGUS_RUN_ROOT to the live run-root ({resolved}); use tmp_path instead")
# postgresql@17 keg (brew). The client tools in /opt/homebrew/bin come from
# libpq and have NO `postgres` server binary, so point at the keg bin. Override
# with ARGUS_PG_BIN. LC_ALL=C avoids the macOS "postmaster became multithreaded
# during startup" abort that happens when the shell has no locale set.
PG_BIN = os.environ.get("ARGUS_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin")
PG_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _migrate(dsn):
    from argus.v2.db import migrate
    with psycopg.connect(dsn) as conn:
        migrate.apply(conn, MIGRATIONS)
        conn.commit()


@pytest.fixture(scope="session")
def pg_dsn(tmp_path_factory):
    # CI / external Postgres: use it directly (the v2-ci job sets this).
    ext = os.environ.get("ARGUS_DB_DSN")
    if ext:
        _migrate(ext)
        yield ext
        return
    # Local: spin an ephemeral cluster. Skip cleanly if no server is installed
    # (e.g. the legacy `pytest -q` CI job on a box without postgres) so the gate
    # is the only thing that *requires* a DB. Under ARGUS_GATE=1 a skip fails the
    # run, which is correct: the gate job always provides a DB.
    if not Path(f"{PG_BIN}/initdb").exists():
        pytest.skip("no postgres server available (set ARGUS_DB_DSN or install postgresql@17)")
    data = tmp_path_factory.mktemp("pgdata")
    # macOS unix-socket path limit is 103 chars; tmp_path_factory paths are too long.
    # Use a short /tmp subdir instead.
    import tempfile
    sock_dir = tempfile.mkdtemp(prefix="pgs", dir="/tmp")
    sock = Path(sock_dir)
    port = _free_port()
    try:
        subprocess.run([f"{PG_BIN}/initdb", "-D", str(data), "-U", "argus",
                        "--auth=trust", "-E", "UTF8", "--locale=C"], check=True,
                       capture_output=True, env=PG_ENV)
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(sock_dir, ignore_errors=True)
        if os.environ.get("ARGUS_GATE") == "1":
            raise
        stderr = (exc.stderr or b"").decode(errors="replace").strip()
        pytest.skip(f"postgres initdb failed: {stderr or exc}")
    proc = subprocess.Popen(
        [f"{PG_BIN}/postgres", "-D", str(data), "-p", str(port),
         "-k", str(sock), "-c", "listen_addresses=127.0.0.1",
         "-c", "fsync=off"],
        env=PG_ENV,
    )
    admin = f"host=127.0.0.1 port={port} dbname=postgres user=argus"
    dsn = f"host=127.0.0.1 port={port} dbname=argus user=argus"
    for _ in range(100):
        try:
            psycopg.connect(admin).close()
            break
        except psycopg.OperationalError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("postgres did not start")
    # initdb -U argus creates the role + the 'postgres' db, NOT a db named
    # argus. Create it before migrating.
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("CREATE DATABASE argus")
    _migrate(dsn)
    yield dsn
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(data, ignore_errors=True)
    shutil.rmtree(sock_dir, ignore_errors=True)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    # Gate enforcement: under ARGUS_GATE=1, any skipped/xfailed test fails the
    # run. Off by default so local iteration can skip freely.
    if os.environ.get("ARGUS_GATE") != "1":
        return
    bad = terminalreporter.stats.get("skipped", []) + terminalreporter.stats.get("xfailed", [])
    if bad:
        raise pytest.UsageError(
            f"v2 gate: {len(bad)} skipped/xfailed test(s) are not allowed under ARGUS_GATE=1")


@pytest.fixture()
def conn(pg_dsn, monkeypatch):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)
    c = psycopg.connect(pg_dsn)
    # Clean slate per test.
    with c.cursor() as cur:
        cur.execute("""
            DO $$ DECLARE t text;
            BEGIN
              FOR t IN SELECT tablename FROM pg_tables WHERE schemaname='public'
                       AND tablename <> 'schema_migrations'
              LOOP EXECUTE format('TRUNCATE TABLE %I CASCADE', t); END LOOP;
            END $$;
        """)
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def cfg():
    from argus.v2.config import loader
    return loader.load(Path(__file__).parent / "fixtures" / "argus.yaml")


@pytest.fixture()
def cfg_project(tmp_path):
    from argus.v2.config import loader
    y = tmp_path / "p.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    project: { repo: /tmp/x, base_branch: main, test_cmd: 'true' }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior], max_iters: 2 }\n"
    )
    return loader.load(y)
