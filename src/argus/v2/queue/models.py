"""Plain data carriers for the queue layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Job:
    id: str
    request_id: Optional[str]
    event_id: Optional[str]
    conversation_id: Optional[str]
    team_id: str
    role: str
    stage: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    claim_token: Optional[str]
    exec_snapshot: dict
    payload: dict


@dataclass
class RunRecord:
    role: str
    engine: str
    status: str
    model: Optional[str] = None
    prompt: Optional[str] = None
    output: Optional[str] = None
    cost_source: Optional[str] = None
    cost_usd: Optional[str] = None


@dataclass
class ActionIntent:
    type: str
    risk: str  # 'reversible_internal' | 'personal_outward' | 'irreversible_outward'
    idempotency_key: str
    destination_ref: Optional[str] = None
    payload: dict = field(default_factory=dict)
