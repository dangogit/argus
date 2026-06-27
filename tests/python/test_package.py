import subprocess
import sys
import tarfile
from pathlib import Path

import argus
from argus.v2 import cli


REPO = Path(__file__).resolve().parents[2]


def test_version_is_exposed():
    assert argus.__version__ == "0.2.0"


def test_pyproject_packages_migrations():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '[tool.setuptools.package-data]' in text
    assert '"migrations/*.sql"' in text


def test_pyproject_uses_spdx_license_metadata():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in text
    assert 'license-files = ["LICENSE"]' in text
    assert "License :: OSI Approved :: MIT License" not in text


def test_migrations_dir_exposes_tracked_sql_files():
    tracked = subprocess.run(
        ["git", "ls-files", "src/argus/v2/db/migrations/*.sql"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    found = sorted(path.name for path in cli._migrations_dir().glob("*.sql"))

    assert found == sorted(Path(path).name for path in tracked)


def test_sdist_contains_public_source_files_and_excludes_local_config(tmp_path):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            str(REPO),
            "--sdist",
            "--outdir",
            str(tmp_path / "dist"),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    [sdist] = (tmp_path / "dist").glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())

    def contains(suffix: str) -> bool:
        return any(name.endswith(suffix) for name in names)

    assert contains("/README.md")
    assert contains("/THIRD_PARTY_NOTICES.md")
    assert contains("/VISION.md")
    assert contains("/docs/public-launch.md")
    assert contains("/docs/assets/argus-banner.png")
    assert contains("/scripts/install.sh")
    assert contains("/scripts/install.ps1")
    assert contains("/scripts/public_launch_check.py")
    assert contains("/skills/argus-live-onboarding/SKILL.md")
    assert contains("/examples/slack-app-manifest.yaml")
    assert contains("/argus.v2.example.yaml")

    assert not contains("/argus.yaml")
    assert not any("/.github/" in name for name in names)
    assert not any("/tests/" in name for name in names)
