import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_tracked_files_do_not_contain_em_dash():
    files = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    offenders = []
    for rel in files:
        path = REPO / rel
        if path.name in {"package-lock.json"}:
            continue
        if b"\xe2\x80\x94" in path.read_bytes():
            offenders.append(rel)

    assert offenders == []
