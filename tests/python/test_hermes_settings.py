# tests/python/test_hermes_settings.py
"""Covers the three-tier hermes settings resolution: env > v2 argus.yaml >
legacy argus.config.yaml. See src/argus/hermes/settings.py."""
from argus.hermes.settings import hermes_setting

_MIN_V2 = """\
company:
  name: testco
  defaults:
    engine: { engine: echo }
    autonomy:
      reversible_internal: auto
      irreversible_outward: approval
teams:
  - name: dev
    roles:
      - { name: manager, kind: front, prompt: "x" }
    pipeline:
      stages: [manager]
"""


def _legacy_cfg(tmp_path, monkeypatch, body):
    f = tmp_path / "argus.config.yaml"
    f.write_text(body)
    monkeypatch.setenv("ARGUS_CONFIG", str(f))


def _v2_cfg(tmp_path, monkeypatch, hermes_yaml=""):
    f = tmp_path / "argus.yaml"
    f.write_text(_MIN_V2 + hermes_yaml)
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(f))


def test_legacy_fallback_when_no_env_and_no_v2(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_CONFIG_V2", raising=False)
    _legacy_cfg(tmp_path, monkeypatch, "hermes:\n  toolset: file,web,memory\n")
    assert hermes_setting("toolset", "hermes.toolset") == "file,web,memory"


def test_v2_config_wins_over_legacy(tmp_path, monkeypatch):
    _legacy_cfg(tmp_path, monkeypatch, "hermes:\n  toolset: legacy-toolset\n")
    _v2_cfg(tmp_path, monkeypatch, "hermes:\n  toolset: v2-toolset\n")
    assert hermes_setting("toolset", "hermes.toolset") == "v2-toolset"


def test_v2_config_present_but_unset_falls_back_to_legacy(tmp_path, monkeypatch):
    _legacy_cfg(tmp_path, monkeypatch, "hermes:\n  toolset: legacy-toolset\n")
    _v2_cfg(tmp_path, monkeypatch)  # no hermes: section at all
    assert hermes_setting("toolset", "hermes.toolset") == "legacy-toolset"


def test_broken_v2_config_falls_back_to_legacy(tmp_path, monkeypatch):
    """A v2 config that fails to load (missing required fields, bad yaml) must
    never break hermes settings resolution: only skip that tier."""
    _legacy_cfg(tmp_path, monkeypatch, "hermes:\n  toolset: legacy-toolset\n")
    broken = tmp_path / "broken.yaml"
    broken.write_text("not: {valid: [config")
    monkeypatch.setenv("ARGUS_CONFIG_V2", str(broken))
    assert hermes_setting("toolset", "hermes.toolset") == "legacy-toolset"


def test_no_config_anywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_CONFIG_V2", raising=False)
    monkeypatch.setenv("ARGUS_CONFIG", str(tmp_path / "absent.yaml"))
    assert hermes_setting("toolset", "hermes.toolset") is None


def test_env_tier_is_the_callers_responsibility(tmp_path, monkeypatch):
    """hermes_setting() itself has no env tier: callers with an existing env
    override (e.g. ARGUS_HERMES_TOOLSETS) must check it before calling in, and
    a stray env var of a different name must not leak into resolution."""
    monkeypatch.delenv("ARGUS_CONFIG_V2", raising=False)
    monkeypatch.setenv("ARGUS_HERMES_TOOLSETS", "should-not-be-read-here")
    _legacy_cfg(tmp_path, monkeypatch, "hermes:\n  toolset: legacy-toolset\n")
    assert hermes_setting("toolset", "hermes.toolset") == "legacy-toolset"
