"""Per-request git worktree management. Git only (no network). The worktree
lives under <run_root>/worktrees/<request_id> and is removed on terminal."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from argus.v2.ingress.media import run_root


@dataclass
class Worktree:
    path: str
    branch: str
    repo: str  # absolute path to the main repo


def _git(cwd: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def _wt_path(request_id: str) -> Path:
    return run_root() / "worktrees" / request_id


def _write_worktree_excludes(path: Path) -> None:
    common = _git(str(path), "rev-parse", "--git-common-dir").strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = path / common_path
    exclude = common_path / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    additions = [
        "node_modules",
        "node_modules/",
        "**/node_modules",
        "**/node_modules/",
        ".venv",
        ".venv/",
        "**/.venv",
        "**/.venv/",
    ]
    merged = existing[:]
    for item in additions:
        if item not in merged:
            merged.append(item)
    exclude.write_text("\n".join(merged).rstrip() + "\n")


def _branch_exists(repo: str, branch: str) -> bool:
    r = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
    )
    return r.returncode == 0


def _worktree_for_branch(repo: str, branch: str) -> Path | None:
    out = _git(repo, "worktree", "list", "--porcelain", check=False)
    current: Path | None = None
    wanted = f"refs/heads/{branch}"
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch {wanted}" and current is not None:
            return current
    return None


def _detach_stale_branch_worktree(repo: str, branch: str, target: Path) -> None:
    attached = _worktree_for_branch(repo, branch)
    if attached is None or attached == target:
        return
    _git(repo, "worktree", "remove", "--force", str(attached), check=False)
    _git(repo, "worktree", "prune", check=False)


def create_worktree(project, request_id: str) -> Worktree:
    branch = f"{project.work_branch_prefix}/{request_id}"
    path = _wt_path(request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return Worktree(path=str(path), branch=branch, repo=project.repo)
    base_ref = project.base_branch
    remote = getattr(project, "remote", "origin") or "origin"
    if getattr(project, "allow_network", False):
        _git(project.repo, "fetch", "--prune", remote, project.base_branch, check=False)
    remote_base = f"{remote}/{project.base_branch}"
    if _ref_exists(project.repo, remote_base):
        base_ref = remote_base
    if _branch_exists(project.repo, branch):
        _detach_stale_branch_worktree(project.repo, branch, path)
        _git(project.repo, "worktree", "prune", check=False)
        _git(project.repo, "worktree", "add", str(path), branch)
    else:
        _git(project.repo, "worktree", "add", "-b", branch, str(path), base_ref)
    _write_worktree_excludes(path)
    wt = Worktree(path=str(path), branch=branch, repo=project.repo)
    if getattr(project, "setup_cmd", None):
        # operator-authored config (single-tenant trusted instance), like test_cmd
        r = subprocess.run(project.setup_cmd, shell=True, cwd=str(path),
                           capture_output=True, text=True)
        if r.returncode != 0:
            # Setup failed: tear down the half-built worktree so a retry runs
            # setup again from scratch, and fail loudly. A silently-unsetup
            # worktree makes qa run against a broken tree with no signal.
            remove(wt)
            raise RuntimeError(
                f"setup_cmd failed (exit {r.returncode}): "
                f"{r.stderr.strip() or r.stdout.strip()}")
    return wt


def _ref_exists(repo: str, ref: str) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo,
    )
    return r.returncode == 0


def commit_all(path: str, message: str) -> bool:
    _git(path, "add", "-A")
    status = _git(path, "status", "--porcelain")
    if not status.strip():
        return False
    _git(path, "commit", "-m", message)
    return True


def diff(project, path: str) -> str:
    return _git(path, "diff", f"{project.base_branch}...HEAD")


def remove(worktree: Worktree) -> None:
    """Remove the worktree. Runs `git worktree remove --force` from the main
    repo so it still works after the worktree dir is gone. Falls back to
    shutil.rmtree + git worktree prune if that fails."""
    path = worktree.path
    main_repo = worktree.repo

    r = subprocess.run(
        ["git", "worktree", "remove", "--force", path],
        cwd=main_repo,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or Path(path).exists():
        # Fallback: forcibly delete then prune
        shutil.rmtree(path, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=main_repo,
            capture_output=True,
            text=True,
        )
