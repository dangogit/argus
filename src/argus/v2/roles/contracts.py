"""Parse a role's structured result from engine output (a trailing
`ARGUS_RESULT: {json}` line) and interpret per-role decisions. Tolerant: a
missing/garbled marker yields {} and the interpreters fail safe."""
from __future__ import annotations

import json
from typing import Optional, Tuple

_MARK = "ARGUS_RESULT:"

_CONVERSE_ACTIONS = frozenset({"answer", "dispatch", "ignore"})
_TRIAGE_ACTIONS = frozenset({"investigate", "dispatch", "ignore"})
_RESEARCH_RECOMMENDS = frozenset({"fix", "no_fix"})


def parse_result(text: str) -> dict:
    for line in (text or "").splitlines():
        if line.startswith(_MARK):
            try:
                v = json.loads(line[len(_MARK):].strip())
                return v if isinstance(v, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def converse_decision(parsed: dict) -> Tuple[str, str, str]:
    """Extract the manager's converse decision from a parsed ARGUS_RESULT dict.

    Returns (action, reply, task). Unknown or missing action defaults to
    'ignore' so a garbled result never spams the channel."""
    action = parsed.get("action", "")
    if action not in _CONVERSE_ACTIONS:
        action = "ignore"
    reply = parsed.get("reply", "") or ""
    task = parsed.get("task", "") or ""
    return action, reply, task


def triage_decision(parsed: dict) -> Tuple[str, str]:
    """Manager's triage decision for a monitoring signal.

    Returns (action, task). action in {investigate, dispatch, ignore}; unknown or
    missing defaults to 'ignore' so a garbled result never spawns work. task is
    the dev/research instruction (only meaningful for investigate/dispatch)."""
    action = parsed.get("action", "")
    if action not in _TRIAGE_ACTIONS:
        action = "ignore"
    return action, (parsed.get("task", "") or "")


def research_decision(parsed: dict) -> Tuple[str, str]:
    """Researcher's read-only verdict. Returns (recommend, brief). recommend in
    {fix, no_fix}; unknown/missing defaults to 'no_fix' (never act on a garbled
    brief). brief is a short text the developer reuses instead of re-investigating
    (built from summary/hypothesis/implicated_files when present)."""
    recommend = parsed.get("recommend", "")
    if recommend not in _RESEARCH_RECOMMENDS:
        recommend = "no_fix"
    parts = []
    if parsed.get("summary"):
        parts.append(str(parsed["summary"]))
    if parsed.get("hypothesis"):
        parts.append(f"Hypothesis: {parsed['hypothesis']}")
    files = parsed.get("implicated_files")
    if isinstance(files, list) and files:
        parts.append("Implicated files: " + ", ".join(str(f) for f in files))
    if parsed.get("confidence"):
        parts.append(f"Confidence: {parsed['confidence']}")
    return recommend, "\n".join(parts).strip()


def dev_ready(result: dict) -> bool:
    return bool(result.get("ready", True))  # default ready; qa is the real gate


_POSTGRES_ENV_MARKERS = (
    "no postgres server available",
    "postgres initdb failed",
    "postgres did not start",
    "could not connect to server",
    "connection refused",
    "connection to server at",
    "psycopg.operationalerror",
)
_LOCALHOST_ENV_MARKERS = (
    "localhost",
    "127.0.0.1",
    "::1",
)


def qa_verdict(result: dict, test_exit: Optional[int],
               test_output: str | None = None) -> str:
    """Determine qa verdict.

    Priority:
    1. sandbox/Postgres availability blockers when verification output shows one.
    2. parsed verdict field when present.
    3. test_exit when present: 0 -> pass, non-zero -> fail.
    4. PASS, for advisory or no-project mode with no structured result.
    """
    if qa_environment_blocked(test_output):
        return "blocked"
    v = result.get("verdict")
    if v is not None:
        return "pass" if v == "pass" else "fail"
    if test_exit is not None:
        return "pass" if test_exit == 0 else "fail"
    return "pass"  # no test_cmd and no verdict


def qa_environment_blocked(test_output: str | None) -> bool:
    """True when QA could not exercise DB-backed verification in this sandbox."""
    lowered = (test_output or "").lower()
    if not lowered:
        return False
    has_db_context = "postgres" in lowered or "psycopg" in lowered
    has_localhost_context = any(marker in lowered for marker in _LOCALHOST_ENV_MARKERS)
    if not has_db_context and not has_localhost_context:
        return False
    return any(marker in lowered for marker in _POSTGRES_ENV_MARKERS)


def senior_decision(result: dict) -> str:
    return "approve" if result.get("decision") == "approve" else "changes"
