"""Close code-producing obligations only after PR, deploy, and HTTP proof."""
from __future__ import annotations

import errno
import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from argus.v2.actions.executor import risk_for
from argus.v2.ownership import store
from argus.v2.ownership.github import (
    inspect_deploy as inspect_github_deploy,
    inspect_pr,
)
from argus.v2.ownership.vercel import inspect_deploy as inspect_vercel_deploy
from argus.v2.ownership.policy import assess_pr


_PR_NUMBER = re.compile(r"^[1-9][0-9]*$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ACTION_PENDING = frozenset({
    "proposed", "awaiting_approval", "approved", "executing", "held",
})
_DEPLOY_PENDING = frozenset({
    "QUEUED", "PENDING", "WAITING", "REQUESTED", "IN_PROGRESS",
})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_MAX_URL_LENGTH = 4096
_MAX_DECODE_ROUNDS = 16
_TRANSIENT_ERRNOS = frozenset({
    errno.EAGAIN,
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
    errno.EWOULDBLOCK,
})


@dataclass(frozen=True)
class ReconcileResult:
    obligation_id: str
    status: str
    actions_proposed: int = 0
    completed: int = 0
    blocked: int = 0
    rescheduled: int = 0

    @property
    def action_proposed(self) -> bool:
        return self.actions_proposed > 0


@dataclass(frozen=True)
class _Action:
    id: Any
    type: str
    status: str
    provider_ref: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class _PRReference:
    number: int
    repository: tuple[str, str, str] | None


@dataclass(frozen=True)
class _HTTPResponse:
    status_code: int
    headers: dict[str, str]


class PinnedHTTPGet(Protocol):
    """One-hop verifier that must use the supplied IP, TLS name, and Host."""

    def __call__(
        self,
        url: str,
        *,
        connect_ip: str,
        server_hostname: str,
        host_header: str,
        follow_redirects: bool,
        trust_env: bool,
        timeout: float,
    ) -> Any: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        connect_ip: str,
        context: ssl.SSLContext,
        timeout: float,
    ) -> None:
        super().__init__(host, port=443, context=context, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        address = ipaddress.ip_address(self._connect_ip)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        raw_socket = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw_socket.settimeout(self.timeout)
            if self.source_address:
                raw_socket.bind(self.source_address)
            raw_socket.connect((str(address), self.port))
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except BaseException:
            raw_socket.close()
            raise


def reconcile(
    conn: psycopg.Connection,
    cfg,
    obligation,
    *,
    runner,
    http_get: PinnedHTTPGet | None,
    resolver=None,
) -> ReconcileResult:
    """Advance one code-producing obligation by one observable boundary."""
    if conn.autocommit:
        raise ValueError("ownership reconciliation requires autocommit=False")
    current = store.get(conn, obligation.id)
    if current is None:
        raise ValueError(f"obligation not found: {obligation.id}")
    if current.kind not in {"code", "maintenance"} or current.status in {
        "done", "failed",
    }:
        return _result(current)

    try:
        team = cfg.team(current.team_id)
    except KeyError:
        return _block(
            conn, current, f"ownership team is not configured: {current.team_id}")
    if not team.ownership.enabled:
        return _result(current)
    if team.project is None:
        return _block(conn, current, "team project configuration is missing")

    if current.status == "awaiting_pr":
        return _reconcile_open_pr(
            conn, team, current, runner=runner)
    if current.status in {"awaiting_merge", "awaiting_approval"}:
        return _reconcile_merge(
            conn, team, current, runner=runner)
    if current.status == "awaiting_deploy":
        return _reconcile_deploy(
            conn, team, current, runner=runner)
    if current.status == "verifying":
        return _reconcile_smoke(
            conn, team, current, http_get=http_get, resolver=resolver)
    return _result(current)


def _result(
    obligation,
    *,
    actions_proposed: int = 0,
    completed: int = 0,
    blocked: int = 0,
    rescheduled: int = 0,
) -> ReconcileResult:
    return ReconcileResult(
        obligation_id=str(obligation.id),
        status=obligation.status,
        actions_proposed=actions_proposed,
        completed=completed,
        blocked=blocked,
        rescheduled=rescheduled,
    )


def _linked_action(conn: psycopg.Connection, action_id) -> _Action | None:
    if action_id is None:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, type, status, provider_ref, payload "
            "FROM actions WHERE id=%s",
            (action_id,),
        )
        row = cur.fetchone()
    return _Action(
        id=row["id"],
        type=str(row["type"]),
        status=str(row["status"]),
        provider_ref=row["provider_ref"],
        payload=dict(row["payload"] or {}),
    ) if row else None


def _action_error(action: _Action) -> str:
    value = action.payload.get("error")
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())[:1000]
    return "no canonical error was recorded"


def _failed_action_result(conn, obligation, action: _Action) -> ReconcileResult | None:
    if action.status == "failed":
        error = _action_error(action)
        return _block(
            conn,
            obligation,
            f"{action.type} action failed: {error}",
            evidence={
                "action_id": str(action.id),
                "action_type": action.type,
                "action_error": error,
            },
        )
    if action.status == "rejected":
        return _block(
            conn,
            obligation,
            f"{action.type} action was rejected",
            evidence={
                "action_id": str(action.id),
                "action_type": action.type,
                "action_status": "rejected",
            },
        )
    return None


def _canonical_pr_reference(value) -> _PRReference | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _PRReference(value, None) if value > 0 else None
    text = value.strip() if isinstance(value, str) else ""
    if _PR_NUMBER.fullmatch(text):
        return _PRReference(int(text), None)
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or not _valid_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in parsed.netloc)
    ):
        return None
    parts = parsed.path.split("/")
    if (
        len(parts) != 5
        or not _repository_segment(parts[1])
        or not _repository_segment(parts[2])
        or parts[3] != "pull"
        or not _PR_NUMBER.fullmatch(parts[4])
    ):
        return None
    host = parsed.hostname.lower()
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return _PRReference(
        int(parts[4]),
        (host, unquote(parts[1]).lower(), unquote(parts[2]).lower()),
    )


def _repository_segment(value: str) -> str:
    if not value or _BAD_PERCENT_ESCAPE.search(value):
        return ""
    decoded = unquote(value)
    if (
        not decoded
        or decoded in {".", ".."}
        or "/" in decoded
        or "\\" in decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        or not _REPOSITORY_SEGMENT.fullmatch(decoded)
    ):
        return ""
    return decoded


def _pr_repository_identity(url: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    parts = parsed.path.split("/")
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or not _valid_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in parsed.netloc)
        or len(parts) != 5
        or parts[3] != "pull"
        or not _repository_segment(parts[1])
        or not _repository_segment(parts[2])
        or not _PR_NUMBER.fullmatch(parts[4])
    ):
        return None
    host = parsed.hostname.lower()
    if port is not None:
        host = f"{host}:{port}"
    return host, unquote(parts[1]).lower(), unquote(parts[2]).lower()


def _configured_repository_identity(value: str) -> tuple[str, str, str] | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    host = "github.com"
    path = text
    if "://" in text:
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError:
            return None
        if not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        if port is not None:
            host = f"{host}:{port}"
        path = parsed.path.strip("/")
    elif re.fullmatch(r"[^@/\s]+@[^/:\s]+:.+", text):
        _user, remainder = text.split("@", 1)
        host, path = remainder.split(":", 1)
        host = host.lower()
    else:
        parts = text.strip("/").split("/")
        if len(parts) == 3:
            host, path = parts[0].lower(), "/".join(parts[1:])
    parts = path.strip("/").split("/")
    if len(parts) != 2:
        return None
    owner = _repository_segment(parts[0])
    repo_value = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    repo = _repository_segment(repo_value)
    if not owner or not repo or not host:
        return None
    return host, owner.lower(), repo.lower()


def _checkout_repository_identity(team, runner) -> tuple[str, str, str] | None:
    configured = _configured_repository_identity(team.project.github_repo or "")
    if configured is not None:
        return configured
    try:
        remote = runner(
            ["git", "remote", "get-url", team.project.remote],
            cwd=team.project.repo,
        )
    except Exception:
        return None
    return _configured_repository_identity(str(remote).strip())


def _pr_is_in_scope(configured, pr) -> bool:
    return configured is not None and _pr_repository_identity(pr.url) == configured


def _reconcile_open_pr(conn, team, obligation, *, runner) -> ReconcileResult:
    action = _linked_action(conn, obligation.action_id)
    if action is None or action.type != "open_pr":
        return _block(conn, obligation, "awaiting_pr has no linked open_pr action")
    failed = _failed_action_result(conn, obligation, action)
    if failed is not None:
        return failed
    if action.status != "done":
        return _reschedule(conn, team, obligation)
    reference = _canonical_pr_reference(action.provider_ref)
    if reference is None:
        return _block(
            conn,
            obligation,
            "open_pr action returned an invalid provider reference",
            evidence={"action_id": str(action.id)},
        )
    configured_repository = _checkout_repository_identity(team, runner)
    if configured_repository is None or (
        reference.repository is not None
        and reference.repository != configured_repository
    ):
        return _block(
            conn,
            obligation,
            "open_pr provider reference does not match the configured repository",
            evidence={"action_id": str(action.id)},
        )
    number = reference.number
    try:
        pr = inspect_pr(
            cwd=team.project.repo, pr_ref=str(number), runner=runner)
    except Exception:
        return _reschedule(conn, team, obligation)
    if (
        pr.number != number
        or pr.state == "UNKNOWN"
        or not _pr_is_in_scope(configured_repository, pr)
        or (
            reference.repository is not None
            and _pr_repository_identity(pr.url) != reference.repository
        )
    ):
        return _block(
            conn,
            obligation,
            "GitHub PR inspection is incomplete or mismatched",
            evidence={"pr": number},
        )
    if pr.state == "CLOSED":
        return _block(
            conn, obligation, f"GitHub PR #{number} closed without merge")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET provider_ref=%s, updated_at=clock_timestamp() "
            "WHERE id=%s",
            (str(number), obligation.id),
        )
    updated = store.transition(
        conn,
        obligation.id,
        to_status="awaiting_merge",
        reason="open_pr action completed and PR was inspected",
        evidence={
            "pr": number,
            "pr_url": pr.url,
            "head_sha": pr.head_sha,
        },
    )
    if pr.state == "MERGED":
        return _move_to_deploy(conn, updated, pr)
    return _result(updated)


def _reconcile_merge(conn, team, obligation, *, runner) -> ReconcileResult:
    reference = _canonical_pr_reference(obligation.provider_ref)
    if reference is None:
        return _block(conn, obligation, "obligation has no canonical PR number")
    number = reference.number

    action = _linked_action(conn, obligation.action_id)
    if action is not None and action.type in {"ready_pr", "merge_pr"}:
        failed = _failed_action_result(conn, obligation, action)
        if failed is not None:
            return failed
        if action.status in _ACTION_PENDING:
            return _reschedule(conn, team, obligation)

    configured_repository = _checkout_repository_identity(team, runner)
    if configured_repository is None or (
        reference.repository is not None
        and reference.repository != configured_repository
    ):
        return _block(conn, obligation, "configured repository identity is invalid")
    try:
        pr = inspect_pr(
            cwd=team.project.repo, pr_ref=str(number), runner=runner)
    except Exception:
        return _reschedule(conn, team, obligation)
    if (
        pr.number != number
        or pr.state == "UNKNOWN"
        or not _pr_is_in_scope(configured_repository, pr)
        or (
            reference.repository is not None
            and _pr_repository_identity(pr.url) != reference.repository
        )
    ):
        return _block(
            conn,
            obligation,
            "GitHub PR inspection is incomplete or mismatched",
            evidence={"pr": number},
        )
    if pr.state == "CLOSED":
        return _block(
            conn, obligation, f"GitHub PR #{number} closed without merge")
    if pr.state == "MERGED":
        return _move_to_deploy(conn, obligation, pr)

    decision = assess_pr(team, pr)
    if not decision.allowed:
        return _block(
            conn,
            obligation,
            f"PR policy denied automation: {decision.reason}",
            evidence={
                "policy": decision.evidence_dict(),
                "policy_reason": decision.reason,
            },
        )

    if pr.draft is True:
        if not team.ownership.code.auto_ready:
            return _await_approval(
                conn, team, obligation, pr, action_type="ready_pr",
                policy_evidence=decision.evidence_dict(),
            )
        if action is not None and action.type == "ready_pr" and action.status == "done":
            return _reschedule(conn, team, obligation)
        return _queue_pr_action(
            conn, team, obligation, pr, action_type="ready_pr")
    if pr.draft is not False:
        return _block(conn, obligation, "GitHub PR draft state is unknown")

    if not team.ownership.code.auto_merge:
        return _await_approval(
            conn, team, obligation, pr, action_type="merge_pr",
            policy_evidence=decision.evidence_dict(),
        )
    if action is not None and action.type == "merge_pr" and action.status == "done":
        return _reschedule(conn, team, obligation)
    return _queue_pr_action(
        conn, team, obligation, pr, action_type="merge_pr")


def _move_to_deploy(conn, obligation, pr) -> ReconcileResult:
    if not _OBJECT_ID.fullmatch(pr.merge_sha):
        return _block(
            conn,
            obligation,
            f"merged PR #{pr.number} has no canonical merge commit",
            evidence={"pr": pr.number, "pr_url": pr.url},
        )
    updated = store.transition(
        conn,
        obligation.id,
        to_status="awaiting_deploy",
        reason="GitHub PR merged",
        evidence={
            "pr": pr.number,
            "pr_url": pr.url,
            "merge_sha": pr.merge_sha,
            "deploy_started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return _result(updated)


def _queue_pr_action(conn, team, obligation, pr, *, action_type) -> ReconcileResult:
    # This second assessment is deliberately adjacent to insertion. The state
    # inspected for the action is never authorized solely by an earlier branch.
    decision = assess_pr(team, pr)
    if not decision.allowed:
        return _block(
            conn,
            obligation,
            f"PR policy denied {action_type}: {decision.reason}",
            evidence={
                "policy": decision.evidence_dict(),
                "policy_reason": decision.reason,
            },
        )
    if action_type == "ready_pr" and pr.draft is not True:
        return _block(conn, obligation, "ready_pr requires a draft PR")
    if action_type == "merge_pr" and pr.draft is not False:
        return _block(conn, obligation, "merge_pr requires a non-draft PR")

    if obligation.status == "awaiting_approval":
        obligation = store.transition(
            conn,
            obligation.id,
            to_status="awaiting_merge",
            reason=f"{action_type} automation enabled after approval wait",
            evidence={"policy": decision.evidence_dict()},
        )

    key = f"{action_type}:{obligation.id}:{pr.head_sha}"
    payload = {
        "pr": pr.number,
        "cwd": team.project.repo,
        "expected_head_sha": pr.head_sha,
    }
    canonical_risk = risk_for(action_type)
    inserted = False
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO actions
              (request_id, team_id, type, risk, idempotency_key, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                obligation.request_id,
                obligation.team_id,
                action_type,
                canonical_risk,
                key,
                Jsonb(payload),
            ),
        )
        row = cur.fetchone()
        inserted = row is not None
        if row is None:
            cur.execute(
                "SELECT id, team_id, type, risk, status, provider_ref, payload "
                "FROM actions "
                "WHERE idempotency_key=%s",
                (key,),
            )
            existing = cur.fetchone()
            existing_payload = dict(existing["payload"] or {}) if existing else {}
            target_payload = {
                field: existing_payload.get(field) for field in payload
            }
            extra_fields = set(existing_payload) - set(payload) - {"error"}
            if existing is None or (
                existing["team_id"] != obligation.team_id
                or existing["type"] != action_type
                or existing["risk"] != canonical_risk
                or target_payload != payload
                or extra_fields
            ):
                return _block(
                    conn, obligation,
                    f"idempotency collision for ownership action {key}")
            action_id = existing["id"]
            existing_action = _Action(
                id=existing["id"],
                type=existing["type"],
                status=existing["status"],
                provider_ref=existing["provider_ref"],
                payload=existing_payload,
            )
        else:
            action_id = row["id"]
            existing_action = None

    store.link_action(conn, obligation.id, action_id)
    if existing_action is not None:
        failed = _failed_action_result(conn, obligation, existing_action)
        if failed is not None:
            return failed
    if inserted:
        proposal_evidence = {
            "action_id": str(action_id),
            "policy": decision.evidence_dict(),
            "policy_reason": decision.reason,
        }
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE team_obligations SET evidence=evidence || %s, "
                "updated_at=clock_timestamp() WHERE id=%s",
                (Jsonb(proposal_evidence), obligation.id),
            )
        return _result(
            store.get(conn, obligation.id), actions_proposed=1)
    return _result(store.get(conn, obligation.id))


def _control_destination(team) -> str | None:
    for binding in team.channels:
        if binding.role == "control":
            return f"{binding.type}:{binding.channel_id}"
    return None


def _await_approval(
    conn,
    team,
    obligation,
    pr,
    *,
    action_type,
    policy_evidence,
) -> ReconcileResult:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM team_obligations WHERE id=%s FOR UPDATE",
            (obligation.id,),
        )
        if cur.fetchone() is None:
            raise ValueError(f"obligation not found: {obligation.id}")
    current = store.get(conn, obligation.id)
    if current is None:
        raise ValueError(f"obligation not found: {obligation.id}")
    obligation = current
    if obligation.status not in {
        "awaiting_pr", "awaiting_merge", "awaiting_approval",
    }:
        return _result(obligation)
    destination = _control_destination(team)
    if not destination:
        return _block(
            conn, obligation,
            f"{action_type} requires approval but no control channel is configured")
    key = f"ownership_approval:{obligation.id}:{action_type}:{pr.head_sha}"
    verb = "mark ready" if action_type == "ready_pr" else "merge"
    payload = {
        "text": (
            f"Ownership approval needed for {obligation.team_id}: {verb} "
            f"PR #{pr.number}. {pr.url}"
        ),
        "obligation_id": str(obligation.id),
        "pr": pr.number,
        "action_type": action_type,
    }
    canonical_risk = risk_for("notify")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO actions
              (request_id, team_id, type, risk, destination_ref,
               idempotency_key, payload)
            VALUES (%s, %s, 'notify', %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                obligation.request_id,
                obligation.team_id,
                canonical_risk,
                destination,
                key,
                Jsonb(payload),
            ),
        )
        inserted_row = cur.fetchone()
        inserted = inserted_row is not None
        if not inserted:
            cur.execute(
                """
                SELECT team_id, type, risk, destination_ref,
                       idempotency_key, payload
                FROM actions
                WHERE idempotency_key=%s
                """,
                (key,),
            )
            existing = cur.fetchone()
            if existing is None or (
                existing["team_id"] != obligation.team_id
                or existing["type"] != "notify"
                or existing["risk"] != canonical_risk
                or existing["destination_ref"] != destination
                or existing["idempotency_key"] != key
                or dict(existing["payload"] or {}) != payload
            ):
                return _block(
                    conn,
                    obligation,
                    f"idempotency collision for ownership approval {key}",
                )
    evidence = {
        "approval_action": action_type,
        "approval_notice_key": key,
        "policy": policy_evidence,
    }
    updated = store.transition(
        conn,
        obligation.id,
        to_status="awaiting_approval",
        reason=f"{action_type} automation is disabled",
        evidence=evidence,
    )
    return _result(updated, actions_proposed=int(inserted))


def _reconcile_deploy(conn, team, obligation, *, runner) -> ReconcileResult:
    policy = team.ownership.code
    provider = policy.deploy_provider
    workflow = (policy.deploy_workflow or "").strip()
    project = (policy.deploy_project or "").strip()
    scope = (policy.deploy_scope or "").strip()
    merge_sha = str(obligation.evidence.get("merge_sha") or "")
    if provider == "github" and not workflow:
        return _block(conn, obligation, "deployment workflow is not configured")
    if provider == "vercel" and (not project or not scope):
        return _block(
            conn,
            obligation,
            "Vercel deployment project or scope is not configured",
        )
    if not _OBJECT_ID.fullmatch(merge_sha):
        return _block(conn, obligation, "obligation has no canonical merge commit")
    try:
        if provider == "vercel":
            deploy = inspect_vercel_deploy(
                cwd=team.project.repo,
                project=project,
                scope=scope,
                commit_sha=merge_sha,
                expected_branch=team.project.base_branch,
                auth_mode=policy.deploy_vercel_auth,
                runner=runner,
            )
        else:
            deploy = inspect_github_deploy(
                cwd=team.project.repo,
                workflow=workflow,
                commit_sha=merge_sha,
                runner=runner,
            )
    except Exception:
        return _reschedule(conn, team, obligation)
    if not deploy.found:
        return _reschedule(conn, team, obligation)
    evidence = {
        "deploy_provider": provider,
        "deployment_ref": getattr(deploy, "deployment_ref", None) or deploy.url,
        "deployment_url": deploy.url,
        "deployment_status": deploy.status,
        "deployment_conclusion": deploy.conclusion,
        "merge_sha": merge_sha,
        "workflow": workflow or None,
        "workflow_run_id": getattr(deploy, "run_id", None),
        "workflow_url": deploy.url,
        "workflow_status": deploy.status,
        "workflow_conclusion": deploy.conclusion,
    }
    if deploy.successful:
        updated = store.transition(
            conn,
            obligation.id,
            to_status="verifying",
            reason="deployment succeeded for merge commit",
            evidence=evidence,
        )
        return _result(updated)
    if deploy.failed:
        return _block(
            conn,
            obligation,
            f"deployment failed ({deploy.conclusion}): {deploy.url}",
            evidence=evidence,
        )
    if deploy.status in _DEPLOY_PENDING:
        if _deployment_timed_out(obligation, team.ownership.code.deployment_timeout_minutes):
            return _block(
                conn,
                obligation,
                f"deployment timed out: {deploy.url}",
                evidence=evidence,
            )
        return _reschedule(conn, team, obligation)
    return _block(
        conn,
        obligation,
        f"deployment returned unknown state: {deploy.url}",
        evidence=evidence,
    )


def _deployment_timed_out(obligation, minutes: int) -> bool:
    raw_started = obligation.evidence.get("deploy_started_at")
    if isinstance(raw_started, str):
        try:
            started_at = datetime.fromisoformat(raw_started)
        except ValueError:
            return True
    else:
        started_at = obligation.updated_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)
    return elapsed.total_seconds() >= minutes * 60


def _reconcile_smoke(
    conn,
    team,
    obligation,
    *,
    http_get,
    resolver,
) -> ReconcileResult:
    merge_sha = str(obligation.evidence.get("merge_sha") or "")
    workflow_url = str(obligation.evidence.get("workflow_url") or "")
    if not _OBJECT_ID.fullmatch(merge_sha) or _validated_live_url(workflow_url) is None:
        return _block(
            conn, obligation,
            "smoke verification requires canonical merge and workflow evidence")
    base = _validated_live_url(team.ownership.code.live_url)
    if base is None:
        return _block(conn, obligation, "live_url is not a safe HTTPS URL")
    paths = list(team.ownership.code.smoke_paths)
    if not paths:
        return _block(conn, obligation, "no smoke paths are configured")
    urls: list[str] = []
    for path in paths:
        url = _join_smoke_path(base, path)
        if url is None:
            return _block(conn, obligation, f"unsafe smoke path: {path!r}")
        urls.append(url)

    get = http_get or _pinned_https_get
    resolve = resolver or _default_resolver

    checked_at = datetime.now(timezone.utc).isoformat()
    smoke: list[dict[str, Any]] = []
    failure = ""
    transient = False
    for url in urls:
        response_url, status, error, transient = _request_smoke_url(
            url,
            get=get,
            resolver=resolve,
        )
        if error:
            failure = f"{url}: {error}"
            break
        if not isinstance(status, int) or isinstance(status, bool):
            failure = f"{url}: invalid HTTP status"
            break
        if not 200 <= status < 300:
            failure = f"{url}: HTTP {status}"
            transient = status in {408, 425, 429} or status >= 500
            break
        smoke.append({
            "url": response_url,
            "status": status,
            "workflow_url": str(obligation.evidence.get("workflow_url") or ""),
            "merge_sha": str(obligation.evidence.get("merge_sha") or ""),
            "checked_at": checked_at,
        })
    if not failure:
        updated = store.transition(
            conn,
            obligation.id,
            to_status="done",
            reason="all configured staging smoke checks returned 2xx",
            evidence={"smoke": smoke, "smoke_checked_at": checked_at},
        )
        return _result(updated, completed=1)

    attempted = store.increment_attempts(conn, obligation.id)
    evidence = {
        "smoke_error": failure,
        "smoke_attempt": attempted.attempts,
        "smoke_transient": transient,
        "smoke_checked_at": checked_at,
    }
    if not transient or attempted.attempts >= team.ownership.max_attempts:
        return _block(
            conn,
            attempted,
            f"staging smoke verification failed: {failure}",
            evidence=evidence,
        )
    updated = store.transition(
        conn,
        attempted.id,
        to_status="verifying",
        reason="transient staging smoke failure scheduled for retry",
        evidence=evidence,
    )
    return _reschedule(conn, team, updated)


def _request_smoke_url(url: str, *, get, resolver):
    original = urlsplit(url)
    original_host = original.hostname.lower() if original.hostname else ""
    current = url
    visited: set[str] = set()
    redirects = 0
    while True:
        validated = _validated_redirect_url(current, original_host)
        if validated is None:
            return current, None, "unsafe redirect URL", False
        if validated in visited:
            return validated, None, "redirect loop detected", False
        visited.add(validated)
        addresses, resolution_error = _resolve_public_addresses(
            validated,
            resolver,
        )
        if resolution_error:
            return validated, None, resolution_error, False
        parsed = urlsplit(validated)
        server_hostname = parsed.hostname or ""
        host_header = _host_header(parsed)
        try:
            response = get(
                validated,
                connect_ip=addresses[0],
                server_hostname=server_hostname,
                host_header=host_header,
                follow_redirects=False,
                trust_env=False,
                timeout=15,
            )
        except Exception as exc:
            return validated, None, _clean_error(exc), _is_transient_exception(exc)
        status = getattr(response, "status_code", None)
        if status not in _REDIRECT_STATUSES:
            return validated, status, "", False
        if redirects >= _MAX_REDIRECTS:
            return validated, status, "too many redirects", False
        headers = getattr(response, "headers", None)
        location = headers.get("location") if headers is not None else None
        if location is None and isinstance(headers, dict):
            location = headers.get("Location")
        if not isinstance(location, str) or not location:
            return validated, status, "redirect response has no Location", False
        if (
            len(location) > _MAX_URL_LENGTH
            or "\\" in location
            or any(ord(char) < 32 or ord(char) == 127 for char in location)
        ):
            return validated, status, "unsafe redirect URL", False
        current = urljoin(validated, location)
        if _validated_redirect_url(current, original_host) is None:
            return current, status, "unsafe redirect URL", False
        redirects += 1


def _default_resolver(hostname: str) -> list[str]:
    records = socket.getaddrinfo(
        hostname,
        443,
        type=socket.SOCK_STREAM,
    )
    return sorted({str(record[4][0]) for record in records})


def _resolve_public_addresses(url: str, resolver) -> tuple[list[str], str]:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = list(resolver(hostname))
        except Exception as exc:
            return [], f"host resolution failed: {_clean_error(exc)}"
    if not addresses:
        return [], "host resolution returned no addresses"
    canonical: list[str] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(str(value))
        except ValueError:
            return [], "host resolution returned an invalid IP address"
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            return [], f"host resolved to a non-public address: {address}"
        canonical.append(str(address))
    return sorted(set(canonical)), ""


def _pinned_https_get(
    url: str,
    *,
    connect_ip: str,
    server_hostname: str,
    host_header: str,
    follow_redirects: bool,
    trust_env: bool,
    timeout: float,
) -> _HTTPResponse:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if server_hostname != hostname or host_header != _host_header(parsed):
        raise ValueError("pinned HTTPS connection metadata does not match URL")
    if follow_redirects or trust_env:
        raise ValueError("pinned HTTPS requests cannot use redirects or environment proxies")
    target = parsed.path or "/"
    connection = _PinnedHTTPSConnection(
        hostname,
        connect_ip=connect_ip,
        context=ssl.create_default_context(),
        timeout=timeout,
    )
    try:
        connection.request(
            "GET",
            target,
            headers={"Host": host_header, "Accept": "*/*"},
        )
        response = connection.getresponse()
        headers = {
            str(name).lower(): str(value)
            for name, value in response.getheaders()
        }
        response.read(65536)
        return _HTTPResponse(status_code=response.status, headers=headers)
    finally:
        connection.close()


def _host_header(parsed) -> str:
    hostname = parsed.hostname or ""
    header = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port == 443:
        header = f"{header}:443"
    return header


def _clean_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text or exc.__class__.__name__)[:1000]


def _is_transient_exception(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, socket.gaierror):
        return exc.errno == socket.EAI_AGAIN
    if isinstance(exc, OSError):
        return exc.errno in _TRANSIENT_ERRNOS
    try:
        import httpx
    except Exception:  # pragma: no cover
        return False
    if isinstance(exc, httpx.TimeoutException):
        return True
    cause = exc.__cause__
    return cause is not None and cause is not exc and _is_transient_exception(cause)


def _valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_host) > 253 or ascii_host.endswith("."):
        return False
    return all(_HOST_LABEL.fullmatch(label) for label in ascii_host.split("."))


def _validated_live_url(value) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    try:
        parsed = urlsplit(text)
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or len(text) > _MAX_URL_LENGTH
        or not parsed.hostname
        or not _valid_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
        or "\\" in text
        or not _decoded_path_is_safe(parsed.path)
    ):
        return None
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port == 443:
        host = f"{host}:443"
    normalized_path = parsed.path.rstrip("/")
    if parsed.path and not normalized_path:
        normalized_path = "/"
    return urlunsplit(("https", host, normalized_path, "", ""))


def _validated_redirect_url(value, original_host: str) -> str | None:
    validated = _validated_live_url(value)
    if validated is None:
        return None
    parsed = urlsplit(validated)
    if (parsed.hostname or "").lower() != original_host:
        return None
    return validated or None


def _decoded_path_is_safe(path: str) -> bool:
    if len(path) > _MAX_URL_LENGTH or _BAD_PERCENT_ESCAPE.search(path):
        return False
    decoded = path
    for _ in range(_MAX_DECODE_ROUNDS):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
        if len(decoded) > _MAX_URL_LENGTH:
            return False
    else:
        return False
    if (
        "\\" in decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
    ):
        return False
    return all(segment not in {".", ".."} for segment in decoded.split("/"))


def _join_smoke_path(base: str, value) -> str | None:
    path = value.strip() if isinstance(value, str) else ""
    try:
        parsed_path = urlsplit(path)
    except ValueError:
        return None
    if (
        not path
        or parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.query
        or parsed_path.fragment
        or not _decoded_path_is_safe(parsed_path.path)
    ):
        return None
    parsed_base = urlsplit(base)
    joined = f"{parsed_base.path.rstrip('/')}/{parsed_path.path.lstrip('/')}"
    if path.endswith("/") and not joined.endswith("/"):
        joined += "/"
    return urlunsplit(("https", parsed_base.netloc, joined or "/", "", ""))


def _reschedule(conn, team, obligation) -> ReconcileResult:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE team_obligations
            SET next_check_at=clock_timestamp() + make_interval(secs => %s),
                updated_at=clock_timestamp()
            WHERE id=%s
            """,
            (team.ownership.cycle_seconds, obligation.id),
        )
    return _result(store.get(conn, obligation.id), rescheduled=1)


def _block(
    conn,
    obligation,
    reason: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> ReconcileResult:
    updated = store.transition(
        conn,
        obligation.id,
        to_status="blocked",
        reason=reason,
        evidence=evidence or {},
    )
    return _result(updated, blocked=1)
