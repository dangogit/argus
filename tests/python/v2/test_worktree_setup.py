"""setup_cmd in create_worktree must be bounded by a timeout. A hung install
(stalled npm registry) otherwise hangs the worker thread indefinitely: with
heartbeat renewal keeping the lease alive, the job never reaches lease expiry
on its own, so nothing reclaims it. On timeout the half-built worktree is
removed so a retry re-runs setup from scratch (same contract as a non-zero
exit)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.v2.workspace import repo as workspace_repo


def _mk_repo(tmp_path: Path) -> str:
    r = tmp_path / "mainrepo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=r, check=True,
                   capture_output=True)
    (r / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=r, check=True,
                   capture_output=True)
    return str(r)


def _project(repo: str, **kw) -> SimpleNamespace:
    return SimpleNamespace(repo=repo, base_branch="main",
                           work_branch_prefix="argus/test",
                           allow_network=False, remote="origin", **kw)


def test_setup_cmd_timeout_raises_and_removes_worktree(tmp_path):
    repo = _mk_repo(tmp_path)
    project = _project(repo, setup_cmd="sleep 30", setup_timeout_seconds=1)
    with pytest.raises(RuntimeError, match="setup_cmd timed out"):
        workspace_repo.create_worktree(project, "req-timeout-1")
    assert not (workspace_repo._wt_path("req-timeout-1")).exists()


def test_setup_cmd_within_timeout_succeeds(tmp_path):
    repo = _mk_repo(tmp_path)
    project = _project(repo, setup_cmd="touch installed.marker",
                       setup_timeout_seconds=30)
    wt = workspace_repo.create_worktree(project, "req-ok-1")
    assert (Path(wt.path) / "installed.marker").exists()
    workspace_repo.remove(wt)


def test_setup_cmd_defaults_when_field_absent(tmp_path):
    # Projects built from older configs may lack the attribute entirely;
    # create_worktree must fall back to the 900s default, not crash.
    repo = _mk_repo(tmp_path)
    project = _project(repo, setup_cmd="true")  # no setup_timeout_seconds attr
    wt = workspace_repo.create_worktree(project, "req-default-1")
    assert Path(wt.path).exists()
    workspace_repo.remove(wt)
