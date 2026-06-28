"""Argus host health checks and owner notifications."""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psycopg
import yaml
from psycopg.types.json import Json

from argus.v2 import alerts
from argus.v2.config import loader
from argus.v2.config.schema import Config
from argus.v2.orchestrator import context_router

_ENV_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
_DEFAULT_COOLDOWN_SECONDS = 3600


@dataclass(frozen=True)
class Finding:
    severity: str
    fingerprint: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


def collect_findings(
    *,
    config_path: Path | None = None,
    readiness_failed: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    disk_path: Path | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    if readiness_failed:
        findings.append(Finding(
            severity="error",
            fingerprint="readiness:failed",
            message="readiness check failed",
        ))
    if config_path is not None:
        findings.extend(missing_env_findings(config_path))
    findings.extend(disk_findings(disk_path))
    if sys.platform == "darwin":
        findings.extend(launchd_findings(runner=runner))
    return findings


def missing_env_findings(config_path: Path) -> list[Finding]:
    raw = _load_raw_config(config_path)
    names = sorted({
        name
        for name in _iter_env_refs(raw)
        if not os.environ.get(name)
    })
    return [
        Finding(
            severity="error",
            fingerprint=f"missing-env:{name}",
            message=f"missing env key(s): {name}",
            payload={"env": [name]},
        )
        for name in names
    ]


def disk_findings(path: Path | None = None) -> list[Finding]:
    root = Path(
        path
        or os.environ.get("ARGUS_HEALTH_DISK_PATH")
        or Path.home()
    ).expanduser()
    try:
        usage = shutil.disk_usage(root)
    except Exception as exc:
        return [Finding(
            severity="warn",
            fingerprint=f"disk:unavailable:{_digest(str(root))}",
            message=f"disk check unavailable for {root}: {type(exc).__name__}",
        )]
    min_gb = _float_env("ARGUS_HEALTH_MIN_FREE_GB", 5.0)
    free_gb = usage.free / (1024 ** 3)
    if free_gb >= min_gb:
        return []
    return [Finding(
        severity="warn",
        fingerprint=f"disk:low:{_digest(str(root))}",
        message=f"low disk space under {root}: {free_gb:.1f} GB free",
        payload={"path": str(root), "free_gb": round(free_gb, 2), "min_free_gb": min_gb},
    )]


def keepalive_service_labels(agents_dir: Path | None = None) -> set[str]:
    """The com.argus.* launchd labels whose plist is KeepAlive - the long-running
    services that must stay up (serve, up, postgres, evolution). Periodic jobs
    (poll, retro, watchdog, backup, logrotate; StartInterval) are intentionally
    idle between runs, so we read this to tell the two apart."""
    agents_dir = agents_dir or (Path.home() / "Library" / "LaunchAgents")
    labels: set[str] = set()
    try:
        entries = sorted(agents_dir.glob("com.argus.*.plist"))
    except OSError:
        return labels
    for path in entries:
        try:
            with path.open("rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue
        # KeepAlive may be `true` or a non-empty dict (e.g. {SuccessfulExit:false});
        # both mean "launchd keeps it running", so truthiness is the right test.
        if data.get("KeepAlive"):
            labels.add(path.stem)
    return labels


def launchd_findings(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    service_labels: set[str] | None = None,
) -> list[Finding]:
    """Flag a launchd problem only when a keep-alive SERVICE is down (no PID).

    A periodic job (StartInterval) is supposed to be idle (`pid == "-"`) between
    runs, and `launchctl list` keeps its LAST exit status around - so the old
    "pid == '-' and status != '0'" check re-paged a single transient non-zero
    exit every hour forever, even while the job exited 0 on every real run
    (owner hit this: com.argus.poll status=1 hourly + a long-gone
    support-luma-website). We now page only on a service that should be running
    and isn't; a periodic job's stale exit is no longer a health signal."""
    try:
        proc = runner(["launchctl", "list"], capture_output=True, text=True, timeout=5)
    except Exception as exc:
        return [Finding(
            severity="warn",
            fingerprint="launchd:list-failed",
            message=f"launchd list failed: {type(exc).__name__}",
        )]
    if proc.returncode != 0:
        return [Finding(
            severity="warn",
            fingerprint="launchd:list-failed",
            message="launchd list failed",
        )]
    if service_labels is None:
        service_labels = keepalive_service_labels()
    findings: list[Finding] = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, status, label = parts[0], parts[1], parts[2]
        if label not in service_labels:
            continue  # not a must-stay-up service (periodic job or unknown)
        if pid == "-":
            findings.append(Finding(
                severity="error",
                fingerprint=f"launchd:{label}:down",
                message=f"launchd service down: {label} (not running, last status={status})",
                payload={"label": label, "status": status},
            ))
    return findings


def notify_findings(
    conn: psycopg.Connection,
    cfg: Config,
    findings: list[Finding],
    *,
    cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS,
) -> int:
    if not findings:
        return 0
    destination = general_destination(cfg)
    if not destination:
        return 0

    new_findings: list[Finding] = []
    alert_ids: list[str] = []
    for finding in findings:
        alert_id = alerts.record(
            conn,
            severity=finding.severity,
            project="general",
            fingerprint=finding.fingerprint,
            message=finding.message,
            channel="whatsapp",
            payload=finding.payload,
            cooldown_seconds=cooldown_seconds,
        )
        if alert_id:
            new_findings.append(finding)
            alert_ids.append(alert_id)
    if not new_findings:
        return 0

    text = _format_notification(new_findings)
    idem = f"system_health:{alert_ids[0]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO actions (team_id, type, risk, destination_ref,
                                 idempotency_key, payload)
            VALUES ('general','notify','reversible_internal',%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (destination, idem, Json({"text": text})),
        )
        inserted = int(cur.rowcount)
    if inserted:
        context_router.register_context(
            conn,
            team_id="general",
            channel_ref=destination,
            context_type="system_health",
            context_ref=alert_ids[0],
            summary="; ".join(finding.message for finding in new_findings),
            payload={
                "alert_ids": alert_ids,
                "findings": [
                    {
                        "severity": finding.severity,
                        "fingerprint": finding.fingerprint,
                        "message": finding.message,
                        "payload": finding.payload,
                    }
                    for finding in new_findings
                ],
            },
        )
    return inserted


def check_and_notify(
    conn: psycopg.Connection,
    cfg: Config,
    *,
    config_path: Path | None = None,
    readiness_failed: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    disk_path: Path | None = None,
    cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS,
) -> int:
    findings = collect_findings(
        config_path=config_path,
        readiness_failed=readiness_failed,
        runner=runner,
        disk_path=disk_path,
    )
    return notify_findings(conn, cfg, findings, cooldown_seconds=cooldown_seconds)


def general_destination(cfg: Config) -> str | None:
    try:
        team = cfg.team("general")
    except KeyError:
        return None
    for channel in team.channels:
        if channel.role == "control" and channel.type != "cli":
            return f"{channel.type}:{channel.channel_id}"
    return None


def load_general_routing_config(path: Path) -> Config:
    raw = _load_raw_config(path) or {}
    teams = []
    for team in raw.get("teams") or []:
        if team.get("name") == "general":
            teams.append(team)
            break
    if not teams:
        raise loader.ConfigError("team general missing")
    minimal = {
        "company": raw.get("company") or {"name": "argus"},
        "teams": teams,
    }
    minimal = loader._normalize_raw(minimal)
    cfg = Config.model_validate(minimal)
    team = cfg.team("general")
    for channel in team.channels:
        channel.secret = _resolve_env_ref(channel.secret_ref)
        channel.config = _resolve_present_env_refs(channel.config)
    return cfg


def _load_raw_config(path: Path):
    p = Path(path).expanduser()
    if p.is_dir():
        from argus.v2.config import dir_loader
        return dir_loader.compile(p)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _iter_env_refs(value) -> list[str]:
    if isinstance(value, str):
        match = _ENV_REF.match(value)
        return [match.group(1)] if match else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_iter_env_refs(item))
        return out
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_iter_env_refs(item))
        return out
    return []


def _resolve_env_ref(value: str | None) -> str | None:
    if not value:
        return None
    match = _ENV_REF.match(value)
    if not match:
        return value
    resolved = os.environ.get(match.group(1))
    return resolved or None


def _resolve_present_env_refs(value):
    if isinstance(value, str):
        return _resolve_env_ref(value) or value
    if isinstance(value, list):
        return [_resolve_present_env_refs(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_present_env_refs(item) for key, item in value.items()}
    return value


def _format_notification(findings: list[Finding]) -> str:
    lines = ["Argus health: system issue detected"]
    for finding in findings[:8]:
        lines.append(f"- {finding.message}")
    if len(findings) > 8:
        lines.append(f"- {len(findings) - 8} more issue(s)")
    return "\n".join(lines)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
