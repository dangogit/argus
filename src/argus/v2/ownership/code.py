"""Close code obligations only after PR, deploy, and HTTP proof."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from argus.v2.actions.executor import risk_for
from argus.v2.ownership import store
from argus.v2.ownership.github import inspect_deploy, inspect_pr
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


def reconcile(
    conn: psycopg.Connection,
    cfg,
    obligation,
    *,
    runner,
    http_get,
) -> ReconcileResult:
    """Advance one code obligation by one externally observable boundary."""
    if conn.autocommit:
        raise ValueError("ownership reconciliation requires autocommit=False")
    current = store.get(conn, obligation.id)
    if current is None:
        raise ValueError(f"obligation not found: {obligation.id}")
    if current.kind != "code" or current.status in {"done", "failed"}:
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
            conn, team, current, http_get=http_get)
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


def _canonical_pr_number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = value.strip() if isinstance(value, str) else ""
    if _PR_NUMBER.fullmatch(text):
        return int(text)
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
    return int(parts[4])


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


def _pr_is_in_scope(team, pr) -> bool:
    configured = team.project.github_repo
    if not configured:
        # The numeric gh inspection remains scoped by the configured checkout.
        return True
    return (
        _pr_repository_identity(pr.url)
        == _configured_repository_identity(configured)
    )


def _reconcile_open_pr(conn, team, obligation, *, runner) -> ReconcileResult:
    action = _linked_action(conn, obligation.action_id)
    if action is None or action.type != "open_pr":
        return _block(conn, obligation, "awaiting_pr has no linked open_pr action")
    failed = _failed_action_result(conn, obligation, action)
    if failed is not None:
        return failed
    if action.status != "done":
        return _reschedule(conn, team, obligation)
    number = _canonical_pr_number(action.provider_ref)
    if number is None:
        return _block(
            conn,
            obligation,
            "open_pr action returned an invalid provider reference",
            evidence={"action_id": str(action.id)},
        )
    try:
        pr = inspect_pr(
            cwd=team.project.repo, pr_ref=str(number), runner=runner)
    except Exception:
        return _reschedule(conn, team, obligation)
    if pr.number != number or pr.state == "UNKNOWN" or not _pr_is_in_scope(team, pr):
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
    number = _canonical_pr_number(obligation.provider_ref)
    if number is None:
        return _block(conn, obligation, "obligation has no canonical PR number")

    action = _linked_action(conn, obligation.action_id)
    if action is not None and action.type in {"ready_pr", "merge_pr"}:
        failed = _failed_action_result(conn, obligation, action)
        if failed is not None:
            return failed
        if action.status in _ACTION_PENDING:
            return _reschedule(conn, team, obligation)

    try:
        pr = inspect_pr(
            cwd=team.project.repo, pr_ref=str(number), runner=runner)
    except Exception:
        return _reschedule(conn, team, obligation)
    if pr.number != number or pr.state == "UNKNOWN" or not _pr_is_in_scope(team, pr):
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
    with conn.cursor() as cur:
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
                risk_for("notify"),
                destination,
                key,
                Jsonb(payload),
            ),
        )
        inserted = cur.fetchone() is not None
    evidence = {
        "approval_action": action_type,
        "approval_notice_key": key,
        "policy": policy_evidence,
    }
    updated = _transition_to_approval(
        conn,
        obligation,
        reason=f"{action_type} automation is disabled",
        evidence=evidence,
    )
    return _result(updated, actions_proposed=int(inserted))


def _transition_to_approval(conn, obligation, *, reason, evidence):
    """Record the Task 6 approval edge absent from the Task 2 base map."""
    if obligation.status == "awaiting_approval":
        return obligation
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status FROM team_obligations WHERE id=%s FOR UPDATE",
            (obligation.id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"obligation not found: {obligation.id}")
        from_status = row["status"]
        if from_status == "awaiting_approval":
            return store.get(conn, obligation.id)
        if from_status not in {"awaiting_pr", "awaiting_merge"}:
            raise ValueError(
                f"illegal ownership approval transition: {from_status} -> awaiting_approval")
        cur.execute(
            """
            UPDATE team_obligations
            SET status='awaiting_approval', evidence=evidence || %s,
                blocked_reason=NULL, updated_at=clock_timestamp()
            WHERE id=%s
            """,
            (Jsonb(evidence), obligation.id),
        )
        cur.execute(
            """
            INSERT INTO team_obligation_events
              (obligation_id, from_status, to_status, reason, evidence)
            VALUES (%s, %s, 'awaiting_approval', %s, %s)
            """,
            (obligation.id, from_status, reason, Jsonb(evidence)),
        )
    return store.get(conn, obligation.id)


def _reconcile_deploy(conn, team, obligation, *, runner) -> ReconcileResult:
    workflow = (team.ownership.code.deploy_workflow or "").strip()
    merge_sha = str(obligation.evidence.get("merge_sha") or "")
    if not workflow:
        return _block(conn, obligation, "deployment workflow is not configured")
    if not _OBJECT_ID.fullmatch(merge_sha):
        return _block(conn, obligation, "obligation has no canonical merge commit")
    try:
        deploy = inspect_deploy(
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
        "merge_sha": merge_sha,
        "workflow": workflow,
        "workflow_run_id": deploy.run_id,
        "workflow_url": deploy.url,
        "workflow_status": deploy.status,
        "workflow_conclusion": deploy.conclusion,
    }
    if deploy.successful:
        updated = store.transition(
            conn,
            obligation.id,
            to_status="verifying",
            reason="deployment workflow succeeded for merge commit",
            evidence=evidence,
        )
        return _result(updated)
    if deploy.failed:
        return _block(
            conn,
            obligation,
            f"deployment workflow failed ({deploy.conclusion}): {deploy.url}",
            evidence=evidence,
        )
    if deploy.status in _DEPLOY_PENDING:
        if _deployment_timed_out(obligation, team.ownership.code.deployment_timeout_minutes):
            return _block(
                conn,
                obligation,
                f"deployment workflow timed out: {deploy.url}",
                evidence=evidence,
            )
        return _reschedule(conn, team, obligation)
    return _block(
        conn,
        obligation,
        f"deployment workflow returned unknown state: {deploy.url}",
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


def _reconcile_smoke(conn, team, obligation, *, http_get) -> ReconcileResult:
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

    get = http_get
    if get is None:  # pragma: no cover - exercised by live owner cycle
        import httpx
        get = httpx.get

    checked_at = datetime.now(timezone.utc).isoformat()
    smoke: list[dict[str, Any]] = []
    failure = ""
    transient = False
    for url in urls:
        try:
            response = get(url, follow_redirects=True, timeout=15)
            status = getattr(response, "status_code", None)
        except Exception as exc:
            failure = f"{url}: {_clean_error(exc)}"
            transient = _is_transient_exception(exc)
            break
        if not isinstance(status, int) or isinstance(status, bool):
            failure = f"{url}: invalid HTTP status"
            break
        if not 200 <= status < 300:
            failure = f"{url}: HTTP {status}"
            transient = status in {408, 425, 429} or status >= 500
            break
        smoke.append({
            "url": url,
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


def _clean_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text or exc.__class__.__name__)[:1000]


def _is_transient_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    try:
        import httpx
    except Exception:  # pragma: no cover
        return False
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


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
        or not parsed.hostname
        or not _valid_hostname(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
        or "\\" in parsed.path
        or not _decoded_path_is_safe(parsed.path)
    ):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _decoded_path_is_safe(path: str) -> bool:
    if _BAD_PERCENT_ESCAPE.search(path):
        return False
    decoded = path
    for _ in range(4):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
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
