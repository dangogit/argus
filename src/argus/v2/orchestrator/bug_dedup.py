"""Company-wide duplicate-work guard for bug-report dispatches.

Real incident (2026-07-01): the same supabase bug_reports row
(284b2295-9309-410f-b167-8fc3a16736d6, "uploaded im...") produced two GitHub
PRs in two different repos: dangogit/tadam #324 and dangogit/tadam-agents
#302. Only tadam-agents has a supabase bug source configured; the tadam PR
most likely came from a human Slack request or a PM dispatch on the tadam
team that happened to reference the same bug id. Net effect: duplicate work,
duplicate PRs, owner confusion.

This module extracts a lightweight bug identifier from either a signal's
payload (the supabase connector's ``row.id`` field) or free task text (a
"bug report <uuid>" / "bug <uuid>" phrase), and checks for an existing
non-terminal request anywhere in the company that already references the
same id. Off by default (``dedup_bug_dispatch``); when off this module is a
no-op so current behavior is unchanged.
"""
from __future__ import annotations

import re
from typing import Optional

import psycopg

# Full UUID, or an 8-hex-char prefix (the short form people paste/say). Kept
# conservative on purpose: this only recognizes an unambiguous hex UUID or
# UUID-prefix token, never a bare short number or word, so it cannot
# misfire on ordinary task text.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_PREFIX = r"[0-9a-fA-F]{8}"
_FIELD_RE = re.compile(rf"^(?:{_UUID}|{_UUID_PREFIX})$")
# "bug report <id>" or "bug <id>", optionally with a ':' or '#' separator
# (e.g. "bug #284b2295", "bug: 284b2295-..."). Requires the literal word
# "bug" immediately before the id so it never matches an unrelated UUID
# elsewhere in a longer message.
_TEXT_RE = re.compile(
    rf"\bbug(?:\s+report)?\s*[:#]?\s*({_UUID}|{_UUID_PREFIX})\b",
    re.IGNORECASE,
)


def _normalize(raw: str) -> str:
    """Canonical form for comparison: lowercase, first 8 hex chars. A full
    UUID and its 8-char prefix must resolve to the same bug_ref so a signal
    payload's full id and a human's "bug 284b2295" shorthand collide."""
    return raw.replace("-", "").lower()[:8]


def extract_bug_ref(*, payload: Optional[dict] = None, text: Optional[str] = None) -> Optional[str]:
    """Best-effort bug identifier extraction, normalized to an 8-char lowercase
    hex prefix for storage/comparison. Checks the signal payload's row id
    first (the supabase connector's most reliable field), then falls back to
    a "bug report <uuid>" / "bug <uuid>" pattern in free task text. Returns
    None when nothing conservative matches (no false positives on ordinary
    tasks that merely contain the word "bug")."""
    payload = payload or {}
    row = payload.get("row")
    if isinstance(row, dict):
        rid = row.get("id")
        if rid is not None and _FIELD_RE.match(str(rid)):
            return _normalize(str(rid))
    bug_id = payload.get("bug_id") or payload.get("bug_report_id")
    if bug_id is not None and _FIELD_RE.match(str(bug_id)):
        return _normalize(str(bug_id))
    if text:
        m = _TEXT_RE.search(text)
        if m:
            return _normalize(m.group(1))
    return None


def dedup_enabled(cfg, team_id: str) -> bool:
    """Team override wins; otherwise the company default. Off unless
    explicitly turned on, so existing installs are unaffected."""
    try:
        team_flag = getattr(cfg.team(team_id), "dedup_bug_dispatch", None)
    except KeyError:
        team_flag = None
    if team_flag is not None:
        return bool(team_flag)
    return bool(getattr(cfg.company.defaults, "dedup_bug_dispatch", False))


def find_duplicate(conn: psycopg.Connection, bug_ref: str) -> Optional[tuple[str, str]]:
    """Find an existing non-terminal request anywhere in the company that
    already carries this bug_ref. Returns (request_id, team_id) or None.
    Non-terminal mirrors requests_active_fingerprint: 'open' or
    'awaiting_approval' only - done/failed/cancelled requests never block a
    new dispatch, however old the bug reference is."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, team_id FROM requests "
            "WHERE bug_ref=%s AND status IN ('open','awaiting_approval') "
            "ORDER BY created_at ASC LIMIT 1",
            (bug_ref,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return str(row[0]), str(row[1])
