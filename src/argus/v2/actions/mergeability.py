"""Pre-propose mergeability check for the open_pr action.

Real-world problem: with multiple work branches all cut from the base at
dispatch time and never rebased, once the owner merges one PR the next one
can go CONFLICTING on GitHub the moment it lands. check() runs right before
open_pr pushes the branch and calls `gh pr create`, so a stale branch either
gets rebased onto the CURRENT remote base first, or the PR still opens but
its title/body warn the owner up front instead of surprising them later on
GitHub.

Mechanism: `git merge-tree --write-tree <base> <branch>` (git >= 2.38) checks
whether the merge is clean WITHOUT touching the working tree or index (exit 0
clean, 1 conflict, anything else an error we treat as "could not determine,
proceed unchecked"). If it conflicts, ONE `git rebase <base>` attempt is
made; success means the work now merges cleanly (continue as normal), failure
means the rebase is aborted immediately (`git rebase --abort`, working tree
never left mid-rebase) and the PR still opens, just flagged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

_CommandRunner = Callable[..., "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class MergeCheck:
    mergeable: bool          # True: clean merge (either from the start, or after rebase)
    rebased: bool            # True: a rebase was attempted and succeeded
    conflict: bool           # True: still conflicts after the rebase attempt (or rebase not possible)
    detail: str = ""         # short human-readable outcome for logging / PR body


def _run(cwd: str, argv: list[str], *, runner=None) -> "subprocess.CompletedProcess":
    if runner is not None:
        return runner(argv, cwd=cwd)
    import subprocess
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def check(cwd: str, *, base: str, remote: str = "origin",
          runner: _CommandRunner | None = None) -> MergeCheck:
    """Check whether HEAD in the worktree at `cwd` merges cleanly into the
    CURRENT `remote/base`, attempting one rebase if it does not. `runner`
    defaults to subprocess.run; tests inject a fake for a real throwaway git
    repo or a stub. Never raises: an unexpected git error (fetch failed, base
    ref does not resolve, merge-tree unavailable) is treated as "could not
    determine, proceed unchecked" (mergeable=True, conflict=False) so a
    mergeability-check bug can never block a PR from opening at all."""
    _run(cwd, ["git", "fetch", remote, base], runner=runner)
    remote_base = f"{remote}/{base}"

    # git merge-tree exits 1 both for a real conflict AND for "ref does not
    # resolve" (e.g. fetch failed, base never pushed): resolve the ref first so
    # those two cases are not conflated as a false conflict report.
    resolve = _run(cwd, ["git", "rev-parse", "--verify", "--quiet", remote_base], runner=runner)
    if resolve.returncode != 0:
        return MergeCheck(mergeable=True, rebased=False, conflict=False,
                          detail=f"{remote_base} does not resolve, proceeding unchecked")

    probe = _run(cwd, ["git", "merge-tree", "--write-tree", remote_base, "HEAD"], runner=runner)
    if probe.returncode == 0:
        return MergeCheck(mergeable=True, rebased=False, conflict=False,
                          detail=f"merges cleanly into {remote_base}")
    if probe.returncode != 1:
        # merge-tree itself errored (e.g. git < 2.38): don't block the PR on a
        # check we could not run.
        return MergeCheck(mergeable=True, rebased=False, conflict=False,
                          detail="mergeability check unavailable, proceeding unchecked")

    rebase = _run(cwd, ["git", "rebase", remote_base], runner=runner)
    if rebase.returncode == 0:
        return MergeCheck(mergeable=True, rebased=True, conflict=False,
                          detail=f"rebased onto {remote_base} to resolve conflicts")

    _run(cwd, ["git", "rebase", "--abort"], runner=runner)
    return MergeCheck(mergeable=False, rebased=False, conflict=True,
                      detail=f"conflicts with {remote_base}; rebase attempted and failed, aborted")
