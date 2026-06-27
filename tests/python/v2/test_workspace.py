import subprocess
from pathlib import Path

import pytest

from argus.v2.workspace import repo
from argus.v2.config.schema import Project


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    r = tmp_path / "proj"
    r.mkdir()
    def g(*a): subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    g("init", "-b", "main")
    g("config", "user.email", "t@t.co"); g("config", "user.name", "t")
    (r / "README.md").write_text("hi\n")
    g("add", "-A"); g("commit", "-m", "init")
    return Project(repo=str(r), base_branch="main", work_branch_prefix="argus/dev")


def test_create_worktree_on_new_branch(git_repo):
    wt = repo.create_worktree(git_repo, "req-1")
    assert Path(wt.path).is_dir()
    assert wt.branch == "argus/dev/req-1"
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt.path,
                          capture_output=True, text=True).stdout.strip()
    assert head == "argus/dev/req-1"
    repo.remove(wt)
    assert not Path(wt.path).exists()


def test_create_worktree_reattaches_existing_branch(git_repo):
    wt = repo.create_worktree(git_repo, "req-existing")
    (Path(wt.path) / "fix.py").write_text("print('fixed')\n")
    repo.commit_all(wt.path, "fix")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt.path,
                          capture_output=True, text=True, check=True).stdout.strip()
    repo.remove(wt)

    restored = repo.create_worktree(git_repo, "req-existing")
    restored_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=restored.path,
                                   capture_output=True, text=True,
                                   check=True).stdout.strip()
    assert restored_head == head
    repo.remove(restored)


def test_create_worktree_moves_branch_from_stale_run_root(git_repo, tmp_path, monkeypatch):
    first_root = tmp_path / "run-a"
    second_root = tmp_path / "run-b"
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(first_root))
    old = repo.create_worktree(git_repo, "req-moved")
    old_path = Path(old.path)
    assert old_path.exists()

    monkeypatch.setenv("ARGUS_RUN_ROOT", str(second_root))
    restored = repo.create_worktree(git_repo, "req-moved")

    assert Path(restored.path).is_dir()
    assert Path(restored.path).parent == second_root / "worktrees"
    assert not old_path.exists()
    repo.remove(restored)


def test_create_worktree_fetches_remote_base_when_network_allowed(git_repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=git_repo.repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=git_repo.repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "scratch"], cwd=git_repo.repo,
                   check=True, capture_output=True)
    (Path(git_repo.repo) / "remote.txt").write_text("remote\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo.repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "remote"], cwd=git_repo.repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=git_repo.repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=git_repo.repo,
                   check=True, capture_output=True)

    git_repo.allow_network = True
    wt = repo.create_worktree(git_repo, "req-remote")

    assert (Path(wt.path) / "remote.txt").read_text() == "remote\n"
    repo.remove(wt)


def test_commit_and_diff(git_repo):
    wt = repo.create_worktree(git_repo, "req-2")
    (Path(wt.path) / "fix.py").write_text("print('fixed')\n")
    assert repo.commit_all(wt.path, "fix") is True
    d = repo.diff(git_repo, wt.path)
    assert "fix.py" in d
    assert repo.commit_all(wt.path, "noop") is False  # nothing to commit
    repo.remove(wt)


def test_setup_cmd_runs_on_create(git_repo, tmp_path):
    git_repo.setup_cmd = "touch SETUP_RAN"
    wt = repo.create_worktree(git_repo, "req-setup")
    assert (Path(wt.path) / "SETUP_RAN").exists()
    repo.remove(wt)


def test_setup_dependency_symlink_is_excluded_from_commit(git_repo, tmp_path):
    deps = tmp_path / "deps"
    deps.mkdir()
    git_repo.setup_cmd = f"ln -s {deps} node_modules"
    wt = repo.create_worktree(git_repo, "req-setup-symlink")

    (Path(wt.path) / "fix.py").write_text("print('fixed')\n")
    assert repo.commit_all(wt.path, "fix") is True
    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=wt.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "fix.py" in tree
    assert "node_modules" not in tree
    repo.remove(wt)


def test_setup_cmd_failure_raises_and_cleans_up(git_repo):
    # A failing setup_cmd must fail loudly (so qa never runs against a broken
    # tree) and tear down the worktree so a retry re-runs setup from scratch.
    git_repo.setup_cmd = "exit 3"
    with pytest.raises(RuntimeError):
        repo.create_worktree(git_repo, "req-setupfail")
    assert not repo._wt_path("req-setupfail").exists()
