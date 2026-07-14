"""Parse a role's structured result from engine output (a trailing
`ARGUS_RESULT: {json}` line) and interpret per-role decisions. Tolerant: a
missing/garbled marker yields {} and the interpreters fail safe."""
from __future__ import annotations

import json
from typing import Optional, Tuple

_MARK = "ARGUS_RESULT:"
_MARK_BARE = "ARGUS_RESULT"

_CONVERSE_ACTIONS = frozenset({"answer", "dispatch", "ignore", "learn"})
_TRIAGE_ACTIONS = frozenset({"investigate", "dispatch", "ignore"})
_RESEARCH_RECOMMENDS = frozenset({"fix", "no_fix"})


def parse_result(text: str) -> dict:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(_MARK):
            raw = line[len(_MARK):].strip()
        elif line.startswith(_MARK_BARE + " "):
            raw = line[len(_MARK_BARE):].strip()
        else:
            continue
        try:
            v = json.loads(raw)
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
    task_key = "guidance" if action == "learn" else "task"
    task = parsed.get(task_key, "") or ""
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


def qa_verdict(result: dict, test_exit: Optional[int]) -> str:
    """Determine qa verdict.

    Priority:
    1. parsed verdict field when present.
    2. test_exit when present: 0 -> pass, non-zero -> fail.
    3. PASS, for advisory or no-project mode with no structured result.
    """
    if result.get("qa_environment_blocker"):
        return "fail"
    v = result.get("verdict")
    if v is not None:
        return "pass" if v == "pass" else "fail"
    if test_exit is not None:
        return "pass" if test_exit == 0 else "fail"
    return "pass"  # no test_cmd and no verdict


def senior_decision(result: dict) -> str:
    return "approve" if result.get("decision") == "approve" else "changes"
