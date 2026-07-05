"""Deterministic run-liveness classification.

A run can end "successfully" yet have done nothing useful: the agent only wrote
a plan, hit a blocker, asked for approval, or tripped a missing credential.
Today those look identical to real work in the queue. This module classifies a
run's final output into a typed liveness state from regex evidence - no second
model call, so it is free, fast, and unit-testable. Callers can surface stuck
runs (log, status, escalation) instead of treating them as done.

Pure by design: `classify` reads only its arguments. Keep it that way so it
stays testable without a DB or an engine.
"""
from __future__ import annotations

import re

# Typed states, ordered loosely from "did real work" to "did not".
PRODUCED = "produced"                # substantive output / a change landed
PLANNING_ONLY = "planning_only"      # described a plan but produced no change
BLOCKED = "blocked"                  # agent says it cannot proceed
APPROVAL_REQUIRED = "approval_required"  # agent is waiting on a human decision
EXTERNAL_BLOCKER = "external_blocker"     # missing creds / permission / file
EMPTY = "empty"                      # no meaningful output

# States that mean the run is not making progress and should be surfaced.
STUCK = frozenset({PLANNING_ONLY, BLOCKED, APPROVAL_REQUIRED, EXTERNAL_BLOCKER, EMPTY})

# Evidence corpora. Checked in precedence order (external > approval > blocked >
# planning). Patterns are intentionally conservative to avoid false positives on
# normal narration; broaden only with a test that pins the new case.
_EXTERNAL_BLOCKER_RE = re.compile(
    r"permission denied|access denied|\b401\b|\b403\b|unauthorized|forbidden"
    r"|missing (?:the )?(?:api[ -]?)?(?:key|token|credential)|not authenticated"
    r"|could not authenticate|no such file|command not found|connection refused",
    re.IGNORECASE,
)
_APPROVAL_RE = re.compile(
    r"await(?:ing)? (?:your )?(?:approval|confirmation|sign[ -]?off)"
    r"|please (?:approve|confirm)|need(?:s)? (?:your )?(?:approval|confirmation|sign[ -]?off)"
    r"|should i (?:proceed|continue|go ahead)|waiting (?:for|on) (?:approval|confirmation|you)",
    re.IGNORECASE,
)
_BLOCKER_RE = re.compile(
    r"blocked by|i (?:cannot|can'?t|am unable to|was unable to)"
    r"|unable to (?:proceed|continue|complete|do)|i'?m stuck|stuck on"
    r"|need(?:s)? (?:access|more information|clarification|help)"
    r"|requires? manual|process timed out|timed out after|timeout|deadline_exceeded"
    r"|exceeded deadline",
    re.IGNORECASE,
)
_PLANNING_RE = re.compile(
    r"here'?s (?:my|the) plan|my (?:plan|approach) is|i (?:will|'?ll|plan to|intend to|propose)"
    r"|next steps?:|step 1\b|first,? i (?:will|'?ll|would)|i would (?:start|begin)",
    re.IGNORECASE,
)


def classify(output: str, *, has_diff: bool | None = None) -> str:
    """Classify a run's final output into a liveness state.

    has_diff: True/False when the role produces code (a builder); None for roles
    with no diff concept (judge, converse). A real diff suppresses
    planning-only - the agent planned, then actually did the work.
    """
    text = (output or "").strip()
    if not text:
        return EMPTY
    if _EXTERNAL_BLOCKER_RE.search(text):
        return EXTERNAL_BLOCKER
    if _APPROVAL_RE.search(text):
        return APPROVAL_REQUIRED
    if _BLOCKER_RE.search(text):
        return BLOCKED
    if _PLANNING_RE.search(text) and not has_diff:
        return PLANNING_ONLY
    return PRODUCED
