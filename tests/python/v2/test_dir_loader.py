"""Agent-as-directory loader: a config dir compiles to the SAME dict as the
equivalent single YAML, and loader.load() accepts a directory path."""
from pathlib import Path

import pytest
import yaml

from argus.v2.config import dir_loader, loader

REPO = Path(__file__).resolve().parents[3]
EXAMPLE_YAML = REPO / "argus.v2.example.yaml"
EXAMPLE_DIR = REPO / "examples" / "config-dir"


def test_reference_dir_compiles_to_example_yaml():
    # The committed reference dir must produce byte-identical structure to the
    # canonical single-file example. This is the regression contract for parity.
    compiled = dir_loader.compile(EXAMPLE_DIR)
    expected = yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))
    assert compiled == expected


def test_compiled_dir_validates_to_config(monkeypatch):
    cfg = loader.load(EXAMPLE_DIR)            # load() detects the directory
    assert cfg.company.name == "mycompany"
    assert [r.prompt for r in cfg.team("dev").roles] == [
        "Triage and route.",
        "Investigate read-only; recommend fix or no_fix.",
        "Implement the change. If a research brief is present, do NOT re-investigate.",
        "Run and judge the tests.", "Review before merge."]


def test_load_dir_equals_load_yaml(monkeypatch):
    assert loader.load(EXAMPLE_DIR).model_dump() == loader.load(EXAMPLE_YAML).model_dump()


def test_inline_prompt_overrides_role_md(tmp_path):
    (tmp_path / "company.yaml").write_text("name: c\n", encoding="utf-8")
    team = tmp_path / "teams" / "t"
    (team / "roles").mkdir(parents=True)
    (team / "team.yaml").write_text(
        "roles:\n  - { name: solo, kind: builder, prompt: inline-wins }\n"
        "pipeline: { stages: [solo] }\n", encoding="utf-8")
    (team / "roles" / "solo.md").write_text("from-file", encoding="utf-8")
    compiled = dir_loader.compile(tmp_path)
    assert compiled["teams"][0]["roles"][0]["prompt"] == "inline-wins"


def test_missing_role_md_raises(tmp_path):
    (tmp_path / "company.yaml").write_text("name: c\n", encoding="utf-8")
    team = tmp_path / "teams" / "t"
    team.mkdir(parents=True)
    (team / "team.yaml").write_text(
        "roles:\n  - { name: solo, kind: builder }\npipeline: { stages: [solo] }\n",
        encoding="utf-8")
    with pytest.raises(loader.ConfigError):
        dir_loader.compile(tmp_path)


def test_missing_company_yaml_raises(tmp_path):
    with pytest.raises(loader.ConfigError):
        dir_loader.compile(tmp_path)
