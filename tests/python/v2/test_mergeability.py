"""Pre-propose mergeability check (see actions/mergeability.py): before open_pr
pushes a branch and calls `gh pr create`, check whether the work merges cleanly
into the CURRENT remote base, rebasing once if it does not. Uses real throwaway
git repos (a local 'remote' + a clone) so `git merge-tree` and `git rebase` run
against a genuine history, not a mock.
"""
import subprocess
from pathlib import Path

import pytest

from argus.v2.actions import handlers, mergeability


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


@pytest.fixture()
def remote_and_clone(tmp_path):
    """A bare 'remote' repo plus a clone with a work branch, so base can move
    on the remote independently of the clone's local view (the real-world
    scenario: two PRs both branch from base, one merges, moving the remote
    base out from under the other)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@t.co")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("line1\nline2\nline3\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "t@t.co")
    _git(clone, "config", "user.name", "t")
    return remote, clone


def test_check_reports_mergeable_when_base_unchanged(remote_and_clone):
    remote, clone = remote_and_clone
    (clone / "feature.py").write_text("print('feature')\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "add feature")

    result = mergeability.check(str(clone), base="main", remote="origin")

    assert result.mergeable is True
    assert result.rebased is False
    assert result.conflict is False


def test_check_reports_mergeable_when_base_moved_without_overlap(remote_and_clone):
    remote, clone = remote_and_clone
    # Work branch touches a NEW file (no overlap with what lands on base).
    (clone / "feature.py").write_text("print('feature')\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "add feature")

    # Meanwhile the base moves forward on the remote with an unrelated change.
    other = clone.parent / "other"
    _git(clone.parent, "clone", str(remote), str(other))
    _git(other, "config", "user.email", "t@t.co")
    _git(other, "config", "user.name", "t")
    (other / "other.py").write_text("print('other')\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "unrelated change")
    _git(other, "push", "origin", "main")

    result = mergeability.check(str(clone), base="main", remote="origin")

    # A new, non-overlapping file merges cleanly with the moved base without
    # needing a rebase at all (git merge-tree already resolves it).
    assert result.mergeable is True
    assert result.conflict is False


def test_check_flags_conflict_and_aborts_rebase_when_unresolvable(remote_and_clone):
    remote, clone = remote_and_clone
    # Work branch edits line1 of README.md.
    (clone / "README.md").write_text("WORK BRANCH CHANGE\nline2\nline3\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "work branch edits line1")

    # A sibling PR merges first: base now has a CONFLICTING edit to the same line.
    other = clone.parent / "other2"
    _git(clone.parent, "clone", str(remote), str(other))
    _git(other, "config", "user.email", "t@t.co")
    _git(other, "config", "user.name", "t")
    (other / "README.md").write_text("MERGED SIBLING CHANGE\nline2\nline3\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "sibling PR merged first")
    _git(other, "push", "origin", "main")

    result = mergeability.check(str(clone), base="main", remote="origin")

    assert result.mergeable is False
    assert result.conflict is True
    # The rebase attempt must be fully aborted: no mid-rebase state left behind,
    # and HEAD is still resolvable (not left mid-rebase/detached-broken).
    assert "rebase" not in _git(clone, "status").stdout.lower()
    git_dir = Path(_git(clone, "rev-parse", "--git-dir").stdout.strip())
    if not git_dir.is_absolute():
        git_dir = clone / git_dir
    assert not (git_dir / "rebase-merge").exists()
    assert not (git_dir / "rebase-apply").exists()
    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    assert head


def test_check_never_raises_when_base_ref_missing(tmp_path):
    """No matching remote ref at all (e.g. fetch failed, repo not pushed yet):
    the check must not crash the open_pr flow, just proceed unchecked."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    result = mergeability.check(str(repo), base="main", remote="origin")

    assert result.mergeable is True
    assert result.conflict is False


# --- injected runner (fast, no real git) ---

def _fake_runner(script):
    """script: list of (argv_prefix_tuple, returncode) consumed in call order."""
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        import subprocess as sp
        for prefix, rc in script:
            if tuple(argv[:len(prefix)]) == prefix:
                return sp.CompletedProcess(argv, rc, stdout="", stderr="")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    runner.calls = calls
    return runner


def test_check_clean_merge_skips_rebase_with_injected_runner():
    runner = _fake_runner([
        (("git", "fetch"), 0),
        (("git", "merge-tree"), 0),
    ])
    result = mergeability.check("/tmp/x", base="main", remote="origin", runner=runner)
    assert result.mergeable is True
    assert result.rebased is False
    assert not any(c[:2] == ["git", "rebase"] for c in runner.calls)


def test_check_merge_tree_error_proceeds_unchecked_with_injected_runner():
    runner = _fake_runner([
        (("git", "fetch"), 0),
        (("git", "merge-tree"), 128),  # e.g. old git / no merge base: real error, not conflict
    ])
    result = mergeability.check("/tmp/x", base="main", remote="origin", runner=runner)
    assert result.mergeable is True
    assert result.conflict is False


def test_check_rebase_success_with_injected_runner():
    runner = _fake_runner([
        (("git", "fetch"), 0),
        (("git", "merge-tree"), 1),
        (("git", "rebase"), 0),
    ])
    result = mergeability.check("/tmp/x", base="main", remote="origin", runner=runner)
    assert result.mergeable is True
    assert result.rebased is True
    assert result.conflict is False


def test_check_rebase_failure_aborts_with_injected_runner():
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        import subprocess as sp
        if argv[:2] == ["git", "fetch"]:
            return sp.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["git", "merge-tree"]:
            return sp.CompletedProcess(argv, 1, "", "")
        if argv[:2] == ["git", "rebase"] and "--abort" not in argv:
            return sp.CompletedProcess(argv, 1, "", "conflict")
        return sp.CompletedProcess(argv, 0, "", "")

    result = mergeability.check("/tmp/x", base="main", remote="origin", runner=runner)
    assert result.mergeable is False
    assert result.conflict is True
    assert ["git", "rebase", "--abort"] in calls


# --- open_pr handler wiring ---

def test_open_pr_prefixes_title_and_notes_conflict_in_body(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers.mergeability, "check", lambda cwd, base, remote, runner=None:
                        mergeability.MergeCheck(mergeable=False, rebased=False, conflict=True,
                                                detail="conflicts with origin/main"))
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        return "https://github.com/o/r/pull/9\n" if argv[:2] == ["gh", "pr"] else ""

    ref = handlers.run("open_pr", {
        "branch": "argus/dev/r1", "base": "main", "remote": "origin",
        "title": "Fix login", "body": "auto fix", "cwd": str(tmp_path),
    }, runner=runner)

    assert ref == "https://github.com/o/r/pull/9"
    create_cmd = calls[1]
    title_idx = create_cmd.index("--title")
    body_idx = create_cmd.index("--body")
    assert create_cmd[title_idx + 1] == "[conflicts] Fix login"
    assert "conflicts with origin/main" in create_cmd[body_idx + 1]
    assert "auto fix" in create_cmd[body_idx + 1]


def test_open_pr_leaves_title_unchanged_when_mergeable(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers.mergeability, "check", lambda cwd, base, remote, runner=None:
                        mergeability.MergeCheck(mergeable=True, rebased=False, conflict=False,
                                                detail="merges cleanly"))
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        return "https://github.com/o/r/pull/10\n" if argv[:2] == ["gh", "pr"] else ""

    handlers.run("open_pr", {
        "branch": "argus/dev/r2", "base": "main", "remote": "origin",
        "title": "Fix login", "body": "auto fix", "cwd": str(tmp_path),
    }, runner=runner)

    create_cmd = calls[1]
    title_idx = create_cmd.index("--title")
    assert create_cmd[title_idx + 1] == "Fix login"


def test_open_pr_skips_mergeability_check_without_cwd(monkeypatch):
    """No cwd (e.g. a legacy test payload): the check is skipped, not crashed."""
    called = []
    monkeypatch.setattr(handlers.mergeability, "check",
                        lambda *a, **k: called.append(1) or mergeability.MergeCheck(True, False, False))
    ref = handlers.run("open_pr", {"branch": "b", "base": "main", "remote": "origin",
                                   "title": "t", "body": "x"},
                       runner=lambda argv, cwd=None: "https://github.com/x/y/pull/7\n")
    assert ref == "https://github.com/x/y/pull/7"
    assert called == []
