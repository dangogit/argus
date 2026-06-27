"""Branch-drift monitor: emit a signal when one branch diverges from another
(e.g. staging behind/ahead of main), in BOTH directions. Uses GitHub's compare
API (remote truth, no local clone/fetch needed).

config (source.config):
  github_repo: "owner/repo"        # required
  base: "main"                     # the reference branch (default: main)
  head: "staging"                  # the branch checked for drift (required)
  behind_threshold: 1              # emit when head is >= this many commits behind base
  ahead_threshold: 10              # emit when head is >= this many commits ahead base (0 = off)
secret: a GitHub token (Authorization: Bearer ...).

Dedup: the fingerprint embeds an escalation TIER of the drift, so a worsening
drift (crossing into a higher tier) re-fires for the PM to re-triage, while a
steady drift inside the same tier is deduped at ingest -- no 5-min re-spam.
"""
from __future__ import annotations

from argus.v2.connectors.base import Signal, register

_TIERS = (1, 3, 10, 30, 100, 300)


def _tier(n: int) -> int:
    """Highest tier <= n (the escalation bucket). 0 when below the first tier."""
    t = 0
    for edge in _TIERS:
        if n >= edge:
            t = edge
    return t


@register
class BranchDriftConnector:
    type = "branch_drift"

    @staticmethod
    def parse(raw, state, *, project, base, head, behind_threshold, ahead_threshold):
        """raw = {"ahead_by": int, "behind_by": int} or None (fetch failed)."""
        if not raw:
            return [], state
        ahead = int(raw.get("ahead_by", 0) or 0)
        behind = int(raw.get("behind_by", 0) or 0)
        over_behind = behind >= behind_threshold
        over_ahead = bool(ahead_threshold) and ahead >= ahead_threshold
        if not (over_behind or over_ahead):
            return [], state
        # Tier on whichever dimension is over threshold (worse one wins).
        tier = max(_tier(behind) if over_behind else 0,
                   _tier(ahead) if over_ahead else 0)
        fp = f"branch-drift-{project}-{head}-vs-{base}-t{tier}"
        parts = []
        if behind:
            parts.append(f"{behind} behind")
        if ahead:
            parts.append(f"{ahead} ahead")
        msg = f"branch {head} is {' / '.join(parts)} of {base} ({project})"
        return [Signal(fingerprint=fp, payload={
            "source": "branch_drift", "severity": "warn", "fingerprint": fp,
            "message": msg, "kind": "branch_drift", "project": project,
            "base": base, "head": head, "ahead": ahead, "behind": behind,
        })], state

    def fetch(self, source, state):  # pragma: no cover
        import httpx
        cfg = source.config or {}
        repo = cfg.get("github_repo") or cfg.get("repo")
        base = cfg.get("base", "main")
        head = cfg.get("head")
        if not (repo and head):
            return None
        headers = {"Accept": "application/vnd.github+json"}
        if getattr(source, "secret", None):
            headers["Authorization"] = f"Bearer {source.secret}"
        try:
            r = httpx.get(
                f"https://api.github.com/repos/{repo}/compare/{base}...{head}",
                headers=headers, timeout=float(cfg.get("timeout", 15)),
                follow_redirects=True)
            r.raise_for_status()
            d = r.json()
            return {"ahead_by": d.get("ahead_by", 0), "behind_by": d.get("behind_by", 0)}
        except (httpx.HTTPError, ValueError):
            return None

    def poll(self, source, state):
        cfg = source.config or {}
        project = cfg.get("project") or getattr(source, "team", None) or getattr(source, "name", "repo")
        return self.parse(
            self.fetch(source, state), state,
            project=project, base=cfg.get("base", "main"), head=cfg.get("head", ""),
            behind_threshold=int(cfg.get("behind_threshold", 1)),
            ahead_threshold=int(cfg.get("ahead_threshold", 0)))
