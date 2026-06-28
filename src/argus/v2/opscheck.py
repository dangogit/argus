"""Deep doctor and go-live checks."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from argus.v2.config import loader
from argus.v2.db import pool

BAD = {"missing_secret", "auth_failed", "blocked"}
QUICK_TUNNEL_HOSTS = ("trycloudflare.com", "ngrok-free.app", "ngrok.io", "loca.lt")
SUPPORT_SOURCE_TYPES = {"support_apps_script", "apps_script_support"}
SLACK_PROOF_WINDOW_MINUTES = 30


@dataclass(frozen=True)
class Check:
    area: str
    name: str
    status: str
    detail: str = ""


def doctor_deep(config_path: Path | None, *, live: bool = False,
                require_source_types: set[str] | None = None,
                require_team_source_types: set[str] | None = None,
                require_each_team_source_types: set[str] | None = None) -> tuple[list[Check], int]:
    checks: list[Check] = []
    if not config_path:
        checks.append(Check("config", "path", "blocked", "ARGUS_CONFIG is not set"))
        return checks, 1
    try:
        cfg = loader.load(config_path)
        checks.append(Check("config", str(config_path), "ok", f"teams={len(cfg.teams)}"))
    except Exception as exc:
        checks.append(_config_error_check(str(config_path), exc))
        return checks, 1
    conn = None
    try:
        conn = pool.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM schema_migrations")
            checks.append(Check("db", "migrations", "ok", str(cur.fetchone()[0])))
    except Exception as exc:
        checks.append(Check("db", "migrations", "blocked", str(exc)))
    finally:
        if conn is not None:
            conn.close()
    checks.extend(_role_engine_checks(cfg))
    checks.extend(_channel_checks(cfg))
    checks.extend(_project_checks(cfg, live=live))
    checks.extend(_required_source_type_checks(cfg, require_source_types or set()))
    checks.extend(_required_each_team_source_type_checks(
        cfg, require_each_team_source_types or set()))
    checks.extend(_required_team_source_type_checks(cfg, require_team_source_types or set()))
    checks.extend(_connector_checks(cfg, live=live))
    checks.extend(_mcp_checks(cfg))
    return checks, _rc(checks)


def _mcp_checks(cfg) -> list[Check]:
    """Validate each configured MCP server (command on PATH, url shape, env
    present). Echo-safe: no live protocol handshake (that is P1)."""
    from argus.v2.mcp import config as mcp_config
    out: list[Check] = []
    for s in getattr(getattr(cfg, "mcp", None), "servers", []) or []:
        errs = mcp_config.validate_server(s)
        if errs:
            out.append(Check("mcp", s.name, "blocked", "; ".join(errs)))
        else:
            out.append(Check("mcp", s.name, "ok", f"transport={s.transport}"))
    return out


def go_live(config_path: Path | None, *, mode: str = "chat-only",
            public_url: str | None = None, serve_url: str = "http://127.0.0.1:8787",
            dev_tunnel: bool = False, skip_pr_smoke: bool = False,
            skip_connectors: set[str] | None = None,
            prove_slack_channels: bool = False,
            fresh_slack_proof: bool = False,
            require_source_types: set[str] | None = None,
            require_team_source_types: set[str] | None = None,
            require_each_team_source_types: set[str] | None = None) -> tuple[list[Check], str, int]:
    checks: list[Check] = []
    if not config_path:
        checks.append(Check("config", "path", "blocked", "ARGUS_CONFIG is not set"))
        return checks, "blocked", 1
    try:
        cfg = loader.load(config_path)
        checks.append(Check("config", str(config_path), "ok", f"teams={len(cfg.teams)}"))
    except Exception as exc:
        checks.append(_config_error_check(str(config_path), exc))
        return checks, "blocked", 1
    conn = None
    try:
        conn = pool.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM schema_migrations")
            count = int(cur.fetchone()[0])
        checks.append(Check("db", "migrations", "ok" if count > 0 else "blocked",
                            f"count={count}"))
        checks.extend(_runtime_checks(serve_url=serve_url))
        checks.append(_public_url_check(public_url or os.environ.get("ARGUS_PUBLIC_URL"),
                                        dev_tunnel=dev_tunnel))
        checks.extend(_role_engine_checks(cfg, manager_only=True))
        checks.extend(_channel_checks(cfg))
        checks.extend(_slack_scope_checks(cfg))
        checks.extend(_slack_proof_checks(conn, cfg, require_fresh=fresh_slack_proof))
        if prove_slack_channels:
            checks.extend(_slack_channel_smoke_checks(cfg))
        checks.extend(_required_source_type_checks(cfg, require_source_types or set()))
        checks.extend(_required_each_team_source_type_checks(
            cfg, require_each_team_source_types or set()))
        checks.extend(_required_team_source_type_checks(cfg, require_team_source_types or set()))
        if mode == "monitor-only":
            checks.extend(_connector_checks(cfg, live=True,
                                            skip=skip_connectors or set(), conn=conn))
        if mode == "pm-propose-pr":
            checks.extend(_pm_checks(cfg, conn=conn, skip_pr_smoke=skip_pr_smoke))
    except Exception as exc:
        checks.append(Check("go-live", "unexpected", "blocked", str(exc)))
    finally:
        if conn is not None:
            conn.close()
    status = _overall_status(checks)
    return checks, status, 0 if status == "operational" else 1


def report_json(checks: list[Check], *, status: str | None = None) -> str:
    data = {
        "checks": [asdict(check) for check in checks],
        "summary": _summary(checks),
    }
    if status:
        data["status"] = status
    return json.dumps(data, sort_keys=True)


def print_checks(checks: list[Check], *, status: str | None = None) -> None:
    for check in checks:
        suffix = f" {check.detail}" if check.detail else ""
        print(f"[{check.status}] {check.area}.{check.name}{suffix}")
    if status:
        print(f"status: {status}")


def _config_error_check(name: str, exc: Exception) -> Check:
    text = str(exc)
    if "unresolved env secret:" in text or "unresolved env config:" in text:
        return Check("config", name, "missing_secret", text)
    return Check("config", name, "blocked", text)


def _role_engine_checks(cfg, *, manager_only: bool = False) -> list[Check]:
    checks: list[Check] = []
    from argus.v2.config.loader import resolve_engine

    for team in cfg.teams:
        for role in team.roles:
            if manager_only and role.name != "manager":
                continue
            spec = resolve_engine(cfg, team.name, role.name)
            checks.append(_engine_check(f"{team.name}.{role.name}", spec.engine))
    if manager_only and not checks:
        checks.append(Check("engine", "manager", "blocked", "no manager role configured"))
    return checks


def _engine_check(name: str, engine: str) -> Check:
    if engine in {"echo", "scripted"}:
        return Check("engine", name, "ok", engine)
    binary = _engine_binary(engine)
    found = shutil.which(binary)
    if not found:
        return Check("engine", name, "auth_failed", f"binary not found: {binary}")
    return Check("engine", name, "ok", f"{engine}={found}")


def _engine_binary(engine: str) -> str:
    if engine == "codex":
        return os.environ.get("ARGUS_CODEX_BIN", "codex")
    if engine == "claude-code":
        return os.environ.get("ARGUS_CLAUDE_CODE_BIN", "claude")
    if engine == "hermes":
        return os.environ.get("ARGUS_HERMES_BIN", "hermes")
    return engine


def _channel_checks(cfg) -> list[Check]:
    import argus.v2.channels  # noqa: F401
    from argus.v2.channels.base import REGISTRY

    checks: list[Check] = []
    seen: dict[tuple[str, str], str] = {}
    for team in cfg.teams:
        if not team.channels:
            checks.append(Check("channel", team.name, "not_configured", "no channels"))
            continue
        for channel in team.channels:
            key = (channel.type, channel.channel_id)
            first_team = seen.get(key)
            if channel.type not in {"cli", "fake"} and first_team:
                checks.append(Check("channel", f"{team.name}.{channel.type}", "blocked",
                                    f"duplicate channel_id also used by {first_team}"))
                continue
            seen[key] = team.name
            if channel.type == "cli" or channel.type in REGISTRY:
                checks.append(Check("channel", f"{team.name}.{channel.type}", "ok",
                                    channel.channel_id))
            else:
                checks.append(Check("channel", f"{team.name}.{channel.type}", "blocked",
                                    "adapter not registered"))
    return checks


def _project_checks(cfg, *, live: bool = False) -> list[Check]:
    checks: list[Check] = []
    for team in cfg.teams:
        project = team.project
        if project is None:
            checks.append(Check("project", team.name, "not_configured", "no project"))
            continue
        repo = Path(project.repo).expanduser()
        if not repo.exists():
            checks.append(Check("project", f"{team.name}.repo", "blocked", str(repo)))
            continue
        checks.append(Check("project", f"{team.name}.repo", "ok", str(repo)))
        checks.append(_git_check(repo, "git", "rev-parse", "--show-toplevel"))
        if project.base_branch:
            ok = _git_ok(repo, "rev-parse", "--verify", "--quiet", project.base_branch)
            if not ok:
                ok = _git_ok(repo, "rev-parse", "--verify", "--quiet",
                             f"origin/{project.base_branch}")
            checks.append(Check("project", f"{team.name}.base_branch",
                                "ok" if ok else "blocked", project.base_branch))
        if project.test_cmd:
            checks.append(_command_check(f"{team.name}.test_cmd", project.test_cmd))
        else:
            checks.append(Check("project", f"{team.name}.test_cmd",
                                "not_configured", "missing test_cmd"))
        if project.github_repo or _git_ok(repo, "remote", "get-url", project.remote):
            checks.append(_gh_check(live=live))
    return checks


def _git_check(repo: Path, name: str, *args: str) -> Check:
    return Check("project", name, "ok" if _git_ok(repo, *args) else "blocked",
                 str(repo))


def _git_ok(repo: Path, *args: str) -> bool:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, check=False, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _command_check(name: str, command: str) -> Check:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return Check("project", name, "blocked", str(exc))
    if not parts:
        return Check("project", name, "blocked", "empty command")
    if shutil.which(parts[0]) or parts[0] in {"true", "false"}:
        return Check("project", name, "ok", command)
    return Check("project", name, "blocked", f"binary not found: {parts[0]}")


def _gh_check(*, live: bool) -> Check:
    binary = shutil.which("gh")
    if not binary:
        return Check("github", "auth", "auth_failed", "binary not found: gh")
    if not live:
        return Check("github", "auth", "ok", binary)
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return _gh_command_check(["gh", "api", "user"], "gh api user")
    return _gh_command_check(["gh", "auth", "status"], "gh auth status")


def _gh_command_check(cmd: list[str], ok_detail: str) -> Check:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("github", "auth", "auth_failed", str(exc))
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return Check("github", "auth", "ok" if proc.returncode == 0 else "auth_failed",
                 ok_detail if proc.returncode == 0 else (detail[0] if detail else ""))


def _connector_checks(cfg, *, live: bool, skip: set[str] | None = None,
                      conn=None) -> list[Check]:
    from argus.v2.connectors import driver
    from argus.v2.connectors.base import REGISTRY

    skip = skip or set()
    checks: list[Check] = []
    sources = list(driver._iter_sources(cfg))
    if not sources:
        return [Check("connector", "sources", "not_configured", "no sources")]
    for source, team in sources:
        if source.name in skip or source.type in skip:
            checks.append(Check("connector", source.name, "skipped", source.type))
            continue
        if source.type in SUPPORT_SOURCE_TYPES:
            checks.append(_support_transport_check(cfg, source, team, live=live))
            continue
        if source.type not in REGISTRY:
            checks.append(Check("connector", source.name, "blocked",
                                f"unknown type: {source.type}"))
            continue
        if not live:
            checks.append(Check("connector", source.name, "ok",
                                f"{source.type}:{team}"))
            continue
        local_conn = conn
        close = False
        previews = []
        try:
            if local_conn is None:
                local_conn = pool.connect()
                close = True
            previews = driver.dry_run(local_conn, cfg, source_names={source.name})
        except Exception as exc:
            checks.append(Check("connector", source.name, "auth_failed", str(exc)))
        finally:
            if close and local_conn is not None:
                local_conn.close()
        for preview in previews:
            status = "ok" if preview.ok else "auth_failed"
            detail = f"{preview.source_type}:{preview.team}"
            if preview.error_type:
                detail = f"{detail} {preview.error_type}"
            checks.append(Check("connector", preview.source, status, detail))
    return checks


def _support_transport_check(cfg, source, team: str, *, live: bool) -> Check:
    try:
        cfg.team(team).role("support")
    except Exception:
        return Check("support", source.name, "blocked", f"{team}: support role missing")
    url = (source.config or {}).get("url")
    if not url:
        return Check("support", source.name, "blocked", f"{team}: config.url missing")
    if not source.secret:
        return Check("support", source.name, "missing_secret", f"{team}: secret missing")
    if not live:
        return Check("support", source.name, "ok", f"{source.type}:{team}")
    try:
        from argus.v2.support.apps_script import AppsScriptTransport

        AppsScriptTransport(
            url=url,
            key=source.secret,
            timeout=float((source.config or {}).get("timeout", 30)),
        ).list_unread(1)
    except Exception as exc:
        return Check("support", source.name, "auth_failed", str(exc))
    return Check("support", source.name, "ok", f"{source.type}:{team}")


def _required_source_type_checks(cfg, required: set[str]) -> list[Check]:
    if not required:
        return []
    from argus.v2.connectors import driver

    counts: dict[str, int] = {}
    for source, _team in driver._iter_sources(cfg):
        counts[source.type] = counts.get(source.type, 0) + 1
    checks: list[Check] = []
    for source_type in sorted(required):
        count = counts.get(source_type, 0)
        checks.append(Check(
            "connector",
            f"required.{source_type}",
            "ok" if count else "blocked",
            f"configured={count}" if count else "no source configured",
        ))
    return checks


def _required_team_source_type_checks(cfg, required: set[str]) -> list[Check]:
    if not required:
        return []
    checks: list[Check] = []
    teams = {team.name: team for team in cfg.teams}
    for item in sorted(required):
        if ":" not in item:
            checks.append(Check("connector", f"required_team.{item}", "blocked",
                                "expected TEAM:TYPE"))
            continue
        team_name, source_type = item.split(":", 1)
        team = teams.get(team_name)
        name = f"required_team.{team_name}.{source_type}"
        if team is None:
            checks.append(Check("connector", name, "blocked", "team not configured"))
            continue
        count = sum(1 for source in team.sources if source.type == source_type)
        checks.append(Check(
            "connector",
            name,
            "ok" if count else "blocked",
            f"configured={count}" if count else "no source configured",
        ))
    return checks


def _required_each_team_source_type_checks(cfg, required: set[str]) -> list[Check]:
    if not required:
        return []
    expanded = {
        f"{team.name}:{source_type}"
        for team in cfg.teams
        for source_type in required
    }
    return _required_team_source_type_checks(cfg, expanded)


def _runtime_checks(*, serve_url: str) -> list[Check]:
    checks = [_serve_check(serve_url)]
    units = _argus_units()
    if units is None:
        checks.append(Check("runtime", "host", "skipped", "host status unavailable"))
        checks.append(Check("runtime", "up", "blocked", "cannot prove argus up"))
        return checks
    serve_ok, serve_detail = units.get("serve", (False, "missing"))
    up_ok, up_detail = units.get("up", (False, "missing"))
    checks.append(Check("runtime", "serve_unit", "ok" if serve_ok else "blocked",
                        serve_detail))
    checks.append(Check("runtime", "up", "ok" if up_ok else "blocked",
                        up_detail))
    return checks


def _serve_check(url: str) -> Check:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=2):
            return Check("runtime", "serve", "ok", url)
    except OSError as exc:
        return Check("runtime", "serve", "blocked", f"{url} {exc}")


def _argus_units() -> dict[str, tuple[bool, str]] | None:
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(["launchctl", "list"], capture_output=True,
                                  text=True, check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return _parse_launchctl(proc.stdout)
    try:
        proc = subprocess.run(["systemctl", "--user", "list-units", "argus-*"],
                              capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {
        "serve": (
            "argus-serve.service" in proc.stdout and "running" in proc.stdout,
            "running" if "argus-serve.service" in proc.stdout and "running" in proc.stdout else "missing",
        ),
        "up": (
            "argus-up.service" in proc.stdout and "running" in proc.stdout,
            "running" if "argus-up.service" in proc.stdout and "running" in proc.stdout else "missing",
        ),
    }


def _parse_launchctl(text: str) -> dict[str, tuple[bool, str]]:
    out = {"serve": (False, "missing"), "up": (False, "missing")}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, _status, label = parts[0], parts[1], parts[2]
        running = pid != "-"
        healthy_status = _status in {"0", "-15"}
        ok = running and healthy_status
        detail = "running" if ok else (
            f"running last_status={_status}" if running else "missing"
        )
        if label.endswith(".serve") and pid != "-":
            out["serve"] = (ok, detail)
        if label.endswith(".up") and pid != "-":
            out["up"] = (ok, detail)
    return out


def _public_url_check(url: str | None, *, dev_tunnel: bool) -> Check:
    if not url:
        if dev_tunnel:
            return Check("webhook", "public_url", "skipped", "dev tunnel allowed")
        return Check("webhook", "public_url", "blocked", "ARGUS_PUBLIC_URL is not set")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    is_quick = any(host.endswith(item) for item in QUICK_TUNNEL_HOSTS)
    if is_quick and not dev_tunnel:
        return Check("webhook", "public_url", "blocked",
                     "quick tunnel requires --dev-tunnel")
    return Check("webhook", "public_url", "ok" if not dev_tunnel else "skipped", url)


def _slack_proof_checks(conn, cfg, *, require_fresh: bool = False) -> list[Check]:
    slack_channels = [
        (team.name, ch)
        for team in cfg.teams
        for ch in team.channels
        if ch.type == "slack"
    ]
    has_fake = any(ch.type == "fake" for team in cfg.teams for ch in team.channels)
    if has_fake and not slack_channels:
        return [
            Check("slack", "event_received", "ok", "fake channel"),
            Check("slack", "reply_sent", "ok", "fake channel"),
        ]
    if not slack_channels:
        return [Check("slack", "channel", "blocked", "no slack channel configured")]
    checks: list[Check] = []
    interval = f"{SLACK_PROOF_WINDOW_MINUTES} minutes"
    with conn.cursor() as cur:
        for team_name, channel in slack_channels:
            name = f"{team_name}.{channel.channel_id}"
            ref = f"slack:{channel.channel_id}"
            cur.execute(
                """
                SELECT received_at
                  FROM events
                 WHERE team_id=%s
                   AND kind='message'
                   AND source=%s
                   AND (NOT %s OR received_at >= now() - %s::interval)
                 ORDER BY received_at DESC
                 LIMIT 1
                """,
                (team_name, ref, require_fresh, interval),
            )
            inbound = cur.fetchone()
            checks.append(Check(
                "slack",
                f"{name}.event_received",
                "ok" if inbound else "blocked",
                str(inbound[0]) if inbound else _slack_proof_missing("event", interval,
                                                                      require_fresh),
            ))
            cur.execute(
                """
                SELECT updated_at
                  FROM actions
                 WHERE team_id=%s
                   AND destination_ref=%s
                   AND status='done'
                   AND (NOT %s OR updated_at >= now() - %s::interval)
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (team_name, ref, require_fresh, interval),
            )
            outbound = cur.fetchone()
            checks.append(Check(
                "slack",
                f"{name}.reply_sent",
                "ok" if outbound else "blocked",
                str(outbound[0]) if outbound else _slack_proof_missing("reply", interval,
                                                                       require_fresh),
            ))
    return checks


def _slack_proof_missing(kind: str, interval: str, require_fresh: bool) -> str:
    noun = "event" if kind == "event" else "reply"
    if require_fresh:
        return f"no {noun} in {interval}"
    return f"no {noun} recorded"


def _slack_scope_checks(cfg) -> list[Check]:
    slack_channels = [
        (team.name, ch)
        for team in cfg.teams
        for ch in team.channels
        if ch.type == "slack"
    ]
    if not slack_channels:
        return []

    import httpx
    checks: list[Check] = []
    for team_name, channel in slack_channels:
        name = f"{team_name}.{channel.channel_id}.history_scope"
        if not channel.secret:
            checks.append(Check("slack", name, "missing_secret", "bot token missing"))
            continue
        try:
            response = httpx.post(
                "https://slack.com/api/conversations.history",
                headers={"Authorization": f"Bearer {channel.secret}"},
                json={"channel": channel.channel_id, "limit": 1},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                checks.append(Check("slack", name, "auth_failed",
                                    f"history {data.get('error', 'unknown_error')}"))
                continue
            checks.append(Check("slack", name, "ok", "conversations.history"))
            checks.append(_slack_info_scope_check(team_name, channel))
        except Exception as exc:
            checks.append(Check("slack", name, "auth_failed", str(exc)))
    return checks


def _slack_info_scope_check(team_name: str, channel) -> Check:
    import httpx

    name = f"{team_name}.{channel.channel_id}.info_scope"
    try:
        response = httpx.post(
            "https://slack.com/api/conversations.info",
            headers={"Authorization": f"Bearer {channel.secret}"},
            data={"channel": channel.channel_id},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return Check("slack", name, "auth_failed", str(exc))
    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        if error == "missing_scope":
            return Check("slack", name, "advisory",
                         "info missing_scope (add channels:read)")
        return Check("slack", name, "auth_failed", f"info {error}")
    channel_name = (data.get("channel") or {}).get("name") or channel.channel_id
    return Check("slack", name, "ok", f"conversations.info #{channel_name}")


def _pm_checks(cfg, *, conn=None, skip_pr_smoke: bool) -> list[Check]:
    checks: list[Check] = []
    for team in cfg.teams:
        if team.project is None:
            continue
        role_names = {role.name for role in team.roles}
        missing = {"developer", "qa", "senior"} - role_names
        if missing:
            checks.append(Check("pm", f"{team.name}.roles", "blocked",
                                "missing " + ",".join(sorted(missing))))
        else:
            checks.append(Check("pm", f"{team.name}.roles", "ok",
                                "developer,qa,senior"))
        if team.project.autofix.draft:
            checks.append(Check("pm", f"{team.name}.draft_pr", "ok", "draft=true"))
        else:
            checks.append(Check("pm", f"{team.name}.draft_pr", "blocked",
                                "draft must be true"))
    if skip_pr_smoke:
        checks.append(Check("pm", "pr_smoke", "skipped", "--skip-pr-smoke"))
    else:
        checks.append(_pm_pr_smoke_check(cfg, conn))
    return checks


def _slack_channel_smoke_checks(cfg) -> list[Check]:
    checks: list[Check] = []
    slack_channels = [
        (team.name, channel)
        for team in cfg.teams
        for channel in team.channels
        if channel.type == "slack"
    ]
    if not slack_channels:
        return [Check("slack", "channel_smoke", "not_configured", "no slack channels")]

    import httpx
    for team_name, channel in slack_channels:
        name = f"{team_name}.{channel.channel_id}"
        if not channel.secret:
            checks.append(Check("slack", name, "missing_secret", "bot token missing"))
            continue
        try:
            post = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {channel.secret}"},
                json={
                    "channel": channel.channel_id,
                    "text": (
                        f"Argus go-live channel check for {team_name}. "
                        "Deleting this message."
                    ),
                },
                timeout=20,
            )
            post.raise_for_status()
            post_data = post.json()
            if not post_data.get("ok"):
                checks.append(Check("slack", name, "auth_failed",
                                    f"post {post_data.get('error', 'unknown_error')}"))
                continue
            ts = str(post_data.get("ts") or "")
            delete = httpx.post(
                "https://slack.com/api/chat.delete",
                headers={"Authorization": f"Bearer {channel.secret}"},
                json={"channel": channel.channel_id, "ts": ts},
                timeout=20,
            )
            delete.raise_for_status()
            delete_data = delete.json()
            if not delete_data.get("ok"):
                checks.append(Check("slack", name, "auth_failed",
                                    f"delete {delete_data.get('error', 'unknown_error')}"))
                continue
            checks.append(Check("slack", name, "ok", "post_delete"))
        except Exception as exc:
            checks.append(Check("slack", name, "auth_failed", str(exc)))
    return checks


def _pm_pr_smoke_check(cfg, conn) -> Check:
    teams = [team.name for team in cfg.teams if team.project is not None]
    if not teams:
        return Check("pm", "pr_smoke", "blocked", "no project teams configured")
    if conn is None:
        return Check("pm", "pr_smoke", "blocked", "database unavailable")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.team_id, a.provider_ref
            FROM actions a
            JOIN requests r ON r.id = a.request_id
            WHERE a.team_id = ANY(%s::text[])
              AND a.type = 'open_pr'
              AND a.status = 'done'
              AND a.provider_ref LIKE 'http%%'
              AND a.updated_at >= now() - interval '24 hours'
              AND r.status = 'done'
            ORDER BY a.updated_at DESC, a.created_at DESC
            LIMIT 1
            """,
            (teams,),
        )
        row = cur.fetchone()
    if row:
        team, ref = row
        return Check("pm", "pr_smoke", "ok", f"{team} {ref}")
    return Check("pm", "pr_smoke", "blocked",
                 "run a safe PM smoke or pass --skip-pr-smoke")


def _summary(checks: list[Check]) -> dict[str, int]:
    out: dict[str, int] = {}
    for check in checks:
        out[check.status] = out.get(check.status, 0) + 1
    return out


def _rc(checks: list[Check]) -> int:
    return 1 if any(check.status in BAD for check in checks) else 0


def _overall_status(checks: list[Check]) -> str:
    if any(check.status in BAD for check in checks):
        return "blocked"
    if any(check.status in {"skipped", "not_configured"} for check in checks):
        return "configured-only"
    return "operational"
