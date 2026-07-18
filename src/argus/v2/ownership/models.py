from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

ObligationKind = Literal["code", "support", "maintenance"]
ObligationStatus = Literal[
    "open",
    "working",
    "awaiting_pr",
    "awaiting_merge",
    "awaiting_deploy",
    "verifying",
    "awaiting_approval",
    "blocked",
    "done",
    "failed",
]

LEGAL_TRANSITIONS: dict[ObligationStatus, frozenset[ObligationStatus]] = {
    "open": frozenset({"working", "awaiting_approval", "blocked", "failed"}),
    "working": frozenset({
        "awaiting_pr",
        "awaiting_deploy",
        "verifying",
        "awaiting_approval",
        "blocked",
        "failed",
    }),
    "awaiting_pr": frozenset({"awaiting_merge", "blocked", "failed"}),
    "awaiting_merge": frozenset({"awaiting_deploy", "verifying", "blocked", "failed"}),
    "awaiting_deploy": frozenset({"verifying", "blocked", "failed"}),
    "verifying": frozenset({"working", "blocked", "done", "failed"}),
    "awaiting_approval": frozenset({
        "working",
        "awaiting_pr",
        "awaiting_merge",
        "awaiting_deploy",
        "verifying",
        "blocked",
        "failed",
    }),
    "blocked": frozenset({"open", "working", "awaiting_approval", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
}


@dataclass(frozen=True)
class Obligation:
    id: UUID
    team_id: str
    kind: ObligationKind
    fingerprint: str
    title: str
    status: ObligationStatus
    priority: int
    request_id: UUID | None
    action_id: UUID | None
    provider_ref: str | None
    source_ref: str | None
    definition_of_done: dict[str, Any]
    evidence: dict[str, Any]
    attempts: int
    next_check_at: datetime
    blocked_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
