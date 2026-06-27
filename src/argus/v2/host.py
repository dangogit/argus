"""Host job manifest and unit rendering for v2."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Job:
    name: str
    kind: str
    command: str
    interval: int | None = None
    calendar: str = ""


def parse_job(path: Path) -> Job:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"invalid job: {path}")
    name = str(raw.get("name") or "")
    kind = str(raw.get("kind") or "")
    command = str(raw.get("command") or "")
    if not name or not kind or not command:
        raise ValueError(f"invalid job: {path} missing name, kind, or command")
    if kind not in {"service", "schedule"}:
        raise ValueError(f"invalid job kind: {kind}")
    interval = raw.get("interval")
    return Job(
        name=name,
        kind=kind,
        command=command,
        interval=int(interval) if interval not in (None, "") else None,
        calendar=str(raw.get("calendar") or ""),
    )


def discover_jobs(dirs: list[Path]) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()
    for directory in dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.yaml")):
            job = parse_job(path)
            if job.name in seen:
                continue
            seen.add(job.name)
            jobs.append(job)
    return jobs


def render_launchd_job(job: Job, *, label_prefix: str = "com.argus") -> bytes:
    data: dict[str, object] = {
        "Label": f"{label_prefix}.{job.name}",
        "ProgramArguments": ["/bin/bash", "-c", job.command],
    }
    if job.kind == "service":
        data["KeepAlive"] = True
        data["RunAtLoad"] = True
    elif job.interval:
        data["StartInterval"] = job.interval
    elif job.calendar:
        hour, minute = _parse_calendar(job.calendar)
        data["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    return plistlib.dumps(data, sort_keys=False)


def render_systemd_service(job: Job) -> str:
    escaped = _systemd_shell_arg(job.command).replace("%", "%%")
    return "\n".join([
        "[Unit]",
        f"Description=Argus job argus-{job.name}",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart=/bin/bash -c {escaped}",
        "Restart=on-failure",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def render_systemd_timer(job: Job) -> str:
    lines = [
        "[Unit]",
        f"Description=Argus timer for argus-{job.name}",
        "",
        "[Timer]",
        f"Unit=argus-{job.name}.service",
    ]
    if job.interval:
        lines += ["OnBootSec=60s", f"OnUnitActiveSec={job.interval}s"]
    elif job.calendar:
        lines.append(f"OnCalendar=*-*-* {job.calendar}:00")
    lines += ["Persistent=true", "", "[Install]", "WantedBy=timers.target", ""]
    return "\n".join(lines)


def write_jobs(jobs: list[Job], out_dir: Path, *, os_name: str | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    os_name = os_name or host_os()
    written: list[Path] = []
    for job in jobs:
        if os_name == "macos":
            path = out_dir / f"com.argus.{job.name}.plist"
            path.write_bytes(render_launchd_job(job))
            written.append(path)
            continue
        service = out_dir / f"argus-{job.name}.service"
        service.write_text(render_systemd_service(job), encoding="utf-8")
        written.append(service)
        if job.kind == "schedule":
            timer = out_dir / f"argus-{job.name}.timer"
            timer.write_text(render_systemd_timer(job), encoding="utf-8")
            written.append(timer)
    return written


def host_os() -> str:
    override = os.environ.get("ARGUS_HOST_OS")
    if override:
        return override
    return "macos" if os.uname().sysname == "Darwin" else "linux"


def install_dir(os_name: str | None = None) -> Path:
    if os.environ.get("ARGUS_INSTALL_DIR"):
        return Path(os.environ["ARGUS_INSTALL_DIR"]).expanduser()
    os_name = os_name or host_os()
    if os_name == "macos":
        return Path.home() / "Library" / "LaunchAgents"
    return Path.home() / ".config" / "systemd" / "user"


def install(jobs: list[Job], *, dry_run: bool = False) -> list[Path]:
    os_name = host_os()
    paths = write_jobs(jobs, install_dir(os_name), os_name=os_name)
    if dry_run:
        return paths
    if os_name == "macos":
        uid = os.getuid()
        for path in paths:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=False)
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        for path in paths:
            if path.suffix == ".timer":
                subprocess.run(["systemctl", "--user", "enable", "--now", path.name], check=False)
    return paths


def uninstall(*, dry_run: bool = False) -> list[Path]:
    os_name = host_os()
    root = install_dir(os_name)
    if os_name == "macos":
        paths = sorted(root.glob("com.argus.*.plist"))
        if not dry_run:
            uid = os.getuid()
            for path in paths:
                subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False)
                path.unlink(missing_ok=True)
        return paths
    paths = sorted(root.glob("argus-*.timer")) + sorted(root.glob("argus-*.service"))
    if not dry_run:
        for path in paths:
            if path.suffix == ".timer":
                subprocess.run(["systemctl", "--user", "disable", "--now", path.name], check=False)
            path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return paths


def status() -> str:
    if host_os() == "macos":
        proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
        lines = [line for line in proc.stdout.splitlines() if "com.argus." in line]
        return "\n".join(lines) if lines else "no argus units loaded"
    proc = subprocess.run(
        ["systemctl", "--user", "list-units", "argus-*"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "no argus units loaded"


def backup(run_root: Path, dest_root: Path, *, db_dsn: str = "") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_root / stamp
    dest.mkdir(parents=True, exist_ok=True)
    if db_dsn and shutil.which("pg_dump"):
        subprocess.run(["pg_dump", db_dsn, "-f", str(dest / "argus.sql")], check=False)
    for name in ("media", "content", "support"):
        src = run_root / name
        if src.exists():
            shutil.copytree(src, dest / name, dirs_exist_ok=True)
    _write_sha256s(dest)
    return dest


def logrotate(log_dir: Path, *, max_bytes: int, retain_days: int) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%F")
    rotated = 0
    for log in log_dir.glob("*.log"):
        if log.stat().st_size < max_bytes:
            continue
        shutil.copy2(log, log.with_name(f"{log.name}.{today}"))
        log.write_text("", encoding="utf-8")
        rotated += 1
    cutoff = datetime.now().timestamp() - (retain_days * 86400)
    for item in log_dir.glob("*.log.*"):
        if item.is_file() and item.stat().st_mtime < cutoff:
            item.unlink()
    return rotated


def _parse_calendar(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def _systemd_shell_arg(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _write_sha256s(root: Path) -> None:
    import hashlib

    lines = []
    for item in sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS"):
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        lines.append(f"{digest}  {item.relative_to(root)}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
