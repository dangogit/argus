"""Render launchd units for the Python Argus runtime."""
from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Unit:
    label: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    keep_alive: bool = False
    run_at_load: bool = False
    start_interval: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None


def render(unit: Unit) -> bytes:
    data: dict[str, object] = {
        "Label": unit.label,
        "ProgramArguments": unit.argv,
    }
    if unit.env:
        data["EnvironmentVariables"] = dict(sorted(unit.env.items()))
    if unit.keep_alive:
        data["KeepAlive"] = True
    if unit.run_at_load:
        data["RunAtLoad"] = True
    if unit.start_interval is not None:
        data["StartInterval"] = unit.start_interval
    if unit.stdout_path:
        data["StandardOutPath"] = unit.stdout_path
    if unit.stderr_path:
        data["StandardErrorPath"] = unit.stderr_path
    return plistlib.dumps(data, sort_keys=False)


def _unit_name(unit: "Unit") -> str:
    return unit.label.split(".")[-1]


def render_systemd(unit: Unit) -> tuple[str, str | None]:
    """Render a Unit as a systemd .service string (+ a .timer string for
    interval units). Mirrors the launchd bundle so Linux gets the same
    opinionated serve/up/poll/retro/watchdog/backup/logrotate set out of the
    box, not just a generic host-job renderer."""
    import shlex
    name = _unit_name(unit)
    # systemd treats '%' as a specifier; escape it as '%%' in env values and the
    # ExecStart line, or a DB password containing '%' is silently mangled.
    def esc(s: str) -> str:
        return s.replace("%", "%%")
    env_lines = [f'Environment="{k}={esc(v)}"' for k, v in sorted(unit.env.items())]
    service_type = "simple" if unit.keep_alive else "oneshot"
    service = [
        "[Unit]",
        f"Description=Argus {unit.label}",
        "",
        "[Service]",
        f"Type={service_type}",
        *env_lines,
        "ExecStart=" + esc(" ".join(shlex.quote(a) for a in unit.argv)),
    ]
    if unit.keep_alive:
        # Only long-running services get [Install]; a timer-driven oneshot is
        # started by its .timer, so enabling its .service would double-fire it.
        service += ["Restart=on-failure", "RestartSec=5",
                    "", "[Install]", "WantedBy=default.target"]
    service.append("")
    timer = None
    if unit.start_interval is not None:
        timer = "\n".join([
            "[Unit]",
            f"Description=Argus timer {unit.label}",
            "",
            "[Timer]",
            f"Unit=argus-{name}.service",
            "OnBootSec=60s",
            f"OnUnitActiveSec={unit.start_interval}s",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])
    return "\n".join(service), timer


def write_units(units: Iterable[Unit], out_dir: Path, *, os_name: str = "macos") -> list[Path]:
    """Write the bundle as launchd plists (macos) or systemd units (linux)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for unit in units:
        if os_name == "linux":
            name = _unit_name(unit)
            service_text, timer_text = render_systemd(unit)
            sp = out_dir / f"argus-{name}.service"
            sp.write_text(service_text, encoding="utf-8")
            os.chmod(sp, 0o600)  # Environment= lines can carry the DB DSN/secrets
            written.append(sp)
            if timer_text is not None:
                tp = out_dir / f"argus-{name}.timer"
                tp.write_text(timer_text, encoding="utf-8")
                written.append(tp)
        else:
            path = out_dir / f"{unit.label}.plist"
            path.write_bytes(render(unit))
            written.append(path)
    return written


def _codex_home() -> str:
    raw = os.environ.get("CODEX_HOME")
    if raw:
        return str(Path(os.path.expandvars(raw)).expanduser())
    fleet = Path.home() / ".codex-fleet"
    return str(fleet if fleet.exists() else Path.home() / ".codex")


def default_units(*, python: str, config: str, db_dsn: str, run_root: str,
                  env_files: list[str], log_dir: str,
                  label_prefix: str = "com.argus") -> list[Unit]:
    base_env = {
        "ARGUS_CONFIG": config,
        "ARGUS_CONFIG_V2": config,
        "ARGUS_RUN_ROOT": run_root,
        "ARGUS_CODEX_STDIN": "1",
        "ARGUS_ENGINE_IGNORE_USER_CONFIG": "1",
        "CODEX_HOME": _codex_home(),
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
    }
    if env_files:
        base_env["ARGUS_ENV_FILES"] = os.pathsep.join(env_files)
    elif db_dsn:
        base_env["ARGUS_DB_DSN"] = db_dsn

    def argv(*args: str) -> list[str]:
        return [python, "-m", "argus.v2.cli", *args]

    def log(name: str) -> str:
        return str(Path(log_dir) / f"{name}.log")

    return [
        Unit(
            label=f"{label_prefix}.serve",
            argv=argv("serve", "--port", "8787"),
            env=base_env,
            keep_alive=True,
            run_at_load=True,
            stdout_path=log("serve"),
            stderr_path=log("serve"),
        ),
        Unit(
            label=f"{label_prefix}.up",
            argv=argv("up", "--poll", "10"),
            env=base_env,
            keep_alive=True,
            run_at_load=True,
            stdout_path=log("up"),
            stderr_path=log("up"),
        ),
        Unit(
            label=f"{label_prefix}.poll",
            argv=argv("poll"),
            env=base_env,
            start_interval=300,
            stdout_path=log("poll"),
            stderr_path=log("poll"),
        ),
        Unit(
            label=f"{label_prefix}.retro",
            argv=argv("retro", "run"),
            env=base_env,
            start_interval=86400,
            stdout_path=log("retro"),
            stderr_path=log("retro"),
        ),
        Unit(
            label=f"{label_prefix}.watchdog",
            argv=argv("host", "watchdog"),
            env=base_env,
            start_interval=300,
            stdout_path=log("watchdog"),
            stderr_path=log("watchdog"),
        ),
        Unit(
            label=f"{label_prefix}.backup",
            argv=argv("host", "backup"),
            env=base_env,
            start_interval=86400,
            stdout_path=log("backup"),
            stderr_path=log("backup"),
        ),
        Unit(
            label=f"{label_prefix}.logrotate",
            argv=argv("host", "logrotate"),
            env=base_env,
            start_interval=86400,
            stdout_path=log("logrotate"),
            stderr_path=log("logrotate"),
        ),
    ]
