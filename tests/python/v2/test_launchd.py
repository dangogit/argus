import plistlib
from pathlib import Path

from argus.v2 import launchd


def test_render_service_unit_has_direct_python_argv_and_env():
    unit = launchd.Unit(
        label="com.argus.up",
        argv=["/venv/bin/python", "-m", "argus.v2.cli", "up", "--poll", "10"],
        env={"ARGUS_CONFIG_V2": "/run/argus.yaml", "ARGUS_ENV_FILES": "/run/secrets.env"},
        keep_alive=True,
        run_at_load=True,
        stdout_path="/logs/up.log",
        stderr_path="/logs/up.log",
    )
    data = plistlib.loads(launchd.render(unit))
    assert data["Label"] == "com.argus.up"
    assert data["ProgramArguments"] == [
        "/venv/bin/python", "-m", "argus.v2.cli", "up", "--poll", "10",
    ]
    assert data["EnvironmentVariables"]["ARGUS_ENV_FILES"] == "/run/secrets.env"
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == "/logs/up.log"


def test_default_units_cover_runtime_without_shell_wrapper():
    units = launchd.default_units(
        python="/venv/bin/python",
        config="/run/argus.yaml",
        db_dsn="host=127.0.0.1 port=5440 dbname=argus user=argus",
        run_root="/run",
        env_files=["/secrets.env", "/runtime.env"],
        log_dir="/logs",
    )
    labels = [u.label for u in units]
    assert labels == [
        "com.argus.serve",
        "com.argus.up",
        "com.argus.poll",
        "com.argus.retro",
        "com.argus.watchdog",
        "com.argus.backup",
        "com.argus.logrotate",
    ]
    assert all(u.argv[:3] == ["/venv/bin/python", "-m", "argus.v2.cli"] for u in units)
    assert all("v2-run.sh" not in part for u in units for part in u.argv)
    assert all(u.env["ARGUS_CODEX_STDIN"] == "1" for u in units)
    assert all("ARGUS_DB_DSN" not in u.env for u in units)
    assert all(u.env["CODEX_HOME"] for u in units)
    assert all(u.env["HOME"] for u in units)
    assert units[0].keep_alive is True
    assert units[1].keep_alive is True
    assert units[2].start_interval == 300
    assert units[3].argv[-2:] == ["retro", "run"]
    assert units[3].start_interval == 86400
    assert units[4].argv[-2:] == ["host", "watchdog"]
    assert units[5].argv[-2:] == ["host", "backup"]
    assert units[6].argv[-2:] == ["host", "logrotate"]


def test_default_units_expands_codex_home(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "$HOME/.codex-fleet")
    units = launchd.default_units(
        python="/venv/bin/python",
        config="/run/argus.yaml",
        db_dsn="host=127.0.0.1 port=5440 dbname=argus user=argus",
        run_root="/run",
        env_files=[],
        log_dir="/logs",
    )
    assert units[0].env["CODEX_HOME"] == str(Path.home() / ".codex-fleet")
    assert units[0].env["ARGUS_DB_DSN"].startswith("host=127.0.0.1")
