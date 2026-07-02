"""Pure run-liveness classifier: deterministic, no DB, no engine."""
from __future__ import annotations

import pytest

from argus.v2.worker import liveness as lv


@pytest.mark.parametrize("output,has_diff,expected", [
    # Empty / whitespace -> EMPTY.
    ("", None, lv.EMPTY),
    ("   \n  ", None, lv.EMPTY),
    # Substantive output, no negative markers -> PRODUCED.
    ("Refactored the auth middleware and added a guard.", None, lv.PRODUCED),
    # Planning language with no diff -> PLANNING_ONLY.
    ("Here's my plan: first I will read the config, then patch it.", False, lv.PLANNING_ONLY),
    # Same planning language but a diff landed -> PRODUCED (planned, then did it).
    ("Here's my plan: first I'll patch it. Done, applied the change.", True, lv.PRODUCED),
    # Blocker language -> BLOCKED.
    ("I am unable to proceed without the schema definition.", False, lv.BLOCKED),
    ("This requires manual intervention; I'm stuck on the migration.", None, lv.BLOCKED),
    ("codex: process timed out after 900s", False, lv.BLOCKED),
    ("deadline_exceeded: machine failed to reach desired state", None, lv.BLOCKED),
    # Approval language -> APPROVAL_REQUIRED.
    ("The change is ready. Please approve before I open the PR.", None, lv.APPROVAL_REQUIRED),
    ("Awaiting your approval to continue.", None, lv.APPROVAL_REQUIRED),
    # External blocker (creds/permission/file) -> EXTERNAL_BLOCKER.
    ("Request failed: 403 Forbidden from the GitHub API.", None, lv.EXTERNAL_BLOCKER),
    ("Error: missing API key for Sentry; cannot fetch issues.", None, lv.EXTERNAL_BLOCKER),
    ("bash: gh: command not found", None, lv.EXTERNAL_BLOCKER),
])
def test_classify_states(output, has_diff, expected):
    assert lv.classify(output, has_diff=has_diff) == expected


def test_precedence_external_beats_approval_beats_blocked():
    # All three signals present: external blocker wins.
    text = "Please approve. I am unable to proceed. Also: permission denied."
    assert lv.classify(text) == lv.EXTERNAL_BLOCKER
    # Approval beats blocked when no external blocker.
    text2 = "Please approve. I am unable to proceed for now."
    assert lv.classify(text2) == lv.APPROVAL_REQUIRED


def test_stuck_set_membership():
    assert lv.PLANNING_ONLY in lv.STUCK
    assert lv.PRODUCED not in lv.STUCK
