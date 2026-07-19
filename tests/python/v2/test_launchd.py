import plistlib
import stat
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
    by_label = {u.label: u for u in units}
    labels = [u.label for u in units]
    assert labels == [
        "com.argus.serve",
        "com.argus.up",
        "com.argus.work-chat",
        "com.argus.work-pipeline",
        "com.argus.poll",
        "com.argus.owner",
        "com.argus.retro",
        "com.argus.memory",
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
    # Orchestrator sweeps only; jobs run in the two keep-alive worker lanes.
    assert "--sweep-only" in by_label["com.argus.up"].argv
    assert by_label["com.argus.up"].keep_alive is True
    assert by_label["com.argus.work-chat"].argv[-5:] == ["worker", "--lane", "chat", "--poll", "2"]
    assert by_label["com.argus.work-chat"].keep_alive is True
    assert by_label["com.argus.work-pipeline"].argv[-5:] == ["worker", "--lane", "pipeline", "--poll", "2"]
    assert by_label["com.argus.work-pipeline"].keep_alive is True
    assert by_label["com.argus.serve"].keep_alive is True
    assert by_label["com.argus.poll"].start_interval == 300
    assert by_label["com.argus.owner"].argv[-3:] == ["owner", "cycle", "--json"]
    assert by_label["com.argus.owner"].start_interval == 300
    assert by_label["com.argus.owner"].env == by_label["com.argus.poll"].env
    assert by_label["com.argus.owner"].stdout_path == "/logs/owner.out.log"
    assert by_label["com.argus.owner"].stderr_path == "/logs/owner.err.log"
    assert by_label["com.argus.owner"].keep_alive is False
    assert by_label["com.argus.owner"].run_at_load is False
    assert by_label["com.argus.retro"].argv[-2:] == ["retro", "run"]
    assert by_label["com.argus.retro"].start_interval == 86400
    assert by_label["com.argus.memory"].argv[-2:] == ["memory", "refresh"]
    assert by_label["com.argus.memory"].start_interval == 86400
    assert by_label["com.argus.watchdog"].argv[-2:] == ["host", "watchdog"]
    assert by_label["com.argus.backup"].argv[-2:] == ["host", "backup"]
    assert by_label["com.argus.logrotate"].argv[-2:] == ["host", "logrotate"]


def test_render_systemd_service_unit():
    unit = launchd.Unit(
        label="com.argus.up",
        argv=["/venv/bin/python", "-m", "argus.v2.cli", "up", "--poll", "10"],
        env={"ARGUS_CONFIG_V2": "/run/argus.yaml"},
        keep_alive=True,
    )
    service, timer = launchd.render_systemd(unit)
    assert "Type=simple" in service
    assert 'Environment="ARGUS_CONFIG_V2=/run/argus.yaml"' in service
    assert "ExecStart=/venv/bin/python -m argus.v2.cli up --poll 10" in service
    assert "Restart=on-failure" in service
    assert timer is None  # a long-running service has no timer


def test_render_systemd_interval_unit_has_timer():
    unit = launchd.Unit(
        label="com.argus.poll",
        argv=["/venv/bin/python", "-m", "argus.v2.cli", "poll"],
        start_interval=300,
    )
    service, timer = launchd.render_systemd(unit)
    assert "Type=oneshot" in service
    assert "Restart=on-failure" not in service
    assert timer is not None
    assert "OnUnitActiveSec=300s" in timer
    assert "Unit=argus-poll.service" in timer


def test_write_units_linux_emits_systemd(tmp_path):
    units = launchd.default_units(
        python="/venv/bin/python", config="/run/argus.yaml",
        db_dsn="host=127.0.0.1 dbname=argus", run_root="/run",
        env_files=[], log_dir="/logs")
    written = launchd.write_units(units, tmp_path, os_name="linux")
    names = sorted(p.name for p in written)
    # 11 services + 7 timers (poll/owner/retro/memory/watchdog/backup/logrotate are interval;
    # serve/up/work-chat/work-pipeline are long-running, no timer).
    assert "argus-serve.service" in names
    assert "argus-poll.service" in names and "argus-poll.timer" in names
    assert "argus-owner.service" in names and "argus-owner.timer" in names
    assert "argus-up.timer" not in names  # up is a long-running service
    assert sum(1 for n in names if n.endswith(".service")) == 11
    assert sum(1 for n in names if n.endswith(".timer")) == 7
    assert names.count("argus-memory.timer") == 1
    assert not any(n.endswith(".plist") for n in names)


def test_write_units_macos_still_plists(tmp_path):
    units = launchd.default_units(
        python="/venv/bin/python", config="/run/argus.yaml",
        db_dsn="host=127.0.0.1 dbname=argus", run_root="/run",
        env_files=[], log_dir="/logs")
    written = launchd.write_units(units, tmp_path, os_name="macos")
    assert all(p.suffix == ".plist" for p in written)
    assert len(written) == 11
    assert sum(1 for path in written if path.name == "com.argus.owner.plist") == 1
    assert sum(1 for path in written if path.name == "com.argus.memory.plist") == 1


def test_owner_units_are_deterministic_and_idempotent(tmp_path):
    units = launchd.default_units(
        python="/venv with space/bin/python",
        config="/run/argus config.yaml",
        db_dsn="host=127.0.0.1 dbname=argus",
        run_root="/run root",
        env_files=["/private/runtime env"],
        log_dir="/log root",
    )

    first = launchd.write_units(units, tmp_path, os_name="linux")
    first_content = {path.name: path.read_bytes() for path in first}
    second = launchd.write_units(units, tmp_path, os_name="linux")
    second_content = {path.name: path.read_bytes() for path in second}

    assert [path.name for path in first] == [path.name for path in second]
    assert first_content == second_content
    owner_service = (tmp_path / "argus-owner.service").read_text()
    owner_timer = (tmp_path / "argus-owner.timer").read_text()
    assert "ExecStart='/venv with space/bin/python' -m argus.v2.cli owner cycle --json" in owner_service
    assert 'Environment="ARGUS_CONFIG=/run/argus config.yaml"' in owner_service
    assert "OnUnitActiveSec=300s" in owner_timer
    assert "Persistent=true" in owner_timer


def test_write_units_macos_plists_are_owner_only(tmp_path):
    """Plist EnvironmentVariables can carry ARGUS_DB_DSN with a password, so
    plists must be 0600 like the systemd services."""
    units = launchd.default_units(
        python="/venv/bin/python", config="/run/argus.yaml",
        db_dsn="host=127.0.0.1 dbname=argus password=hunter2", run_root="/run",
        env_files=[], log_dir="/logs")
    written = launchd.write_units(units, tmp_path, os_name="macos")
    for p in written:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


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


def test_render_systemd_escapes_percent():
    unit = launchd.Unit(
        label="com.argus.up",
        argv=["/p", "-m", "argus.v2.cli", "up"],
        env={"ARGUS_DB_DSN": "host=h password=p%40ss"},
        keep_alive=True)
    service, _ = launchd.render_systemd(unit)
    assert 'Environment="ARGUS_DB_DSN=host=h password=p%%40ss"' in service


def test_render_systemd_interval_has_no_install():
    unit = launchd.Unit(label="com.argus.poll",
                        argv=["/p", "-m", "argus.v2.cli", "poll"], start_interval=300)
    service, _ = launchd.render_systemd(unit)
    assert "[Install]" not in service  # timer-driven oneshot must not be enable-able
