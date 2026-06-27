"""Compile an "agent-as-directory" config tree into the same dict that a single
argus.yaml produces, so `loader.load()` validates either form identically.

Layout::

    <root>/company.yaml                  # the `company:` mapping
    <root>/retro.yaml                    # optional top-level retro config
    <root>/teams/<name>/team.yaml        # the team mapping minus role prompts
    <root>/teams/<name>/roles/<role>.md  # prompt body for that role
    <root>/teams/<name>/skills/*.md      # optional, runtime skill overrides

A role inherits its prompt from `roles/<name>.md` when `team.yaml` omits
`prompt` -- prompts read far better as markdown than as inline YAML strings.
Everything else (engines, pipeline, sources, channels, project, autonomy) stays
structured in `team.yaml`, because it is data, not prose. The compiled dict is
byte-for-byte the YAML form, so secret resolution + the engine cascade in
`loader.load()` run afterward unchanged.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from argus.v2.config.loader import ConfigError


def compile(path) -> dict:
    root = Path(path).expanduser()
    company_file = root / "company.yaml"
    if not company_file.is_file():
        raise ConfigError(f"config dir missing company.yaml: {root}")
    company = yaml.safe_load(company_file.read_text(encoding="utf-8")) or {}
    if isinstance(company.get("company"), dict):   # tolerate a wrapping `company:` key
        company = company["company"]
    retro_file = root / "retro.yaml"
    retro = None
    if retro_file.is_file():
        retro = yaml.safe_load(retro_file.read_text(encoding="utf-8")) or {}
        if isinstance(retro.get("retro"), dict):   # tolerate a wrapping `retro:` key
            retro = retro["retro"]
    teams = []
    teams_dir = root / "teams"
    if teams_dir.is_dir():
        for team_dir in sorted(p for p in teams_dir.iterdir() if p.is_dir()):
            teams.append(_compile_team(team_dir))
    compiled = {"company": company, "teams": teams}
    if retro is not None:
        compiled["retro"] = retro
    return compiled


def _compile_team(team_dir: Path) -> dict:
    team_file = team_dir / "team.yaml"
    if not team_file.is_file():
        raise ConfigError(f"team dir missing team.yaml: {team_dir}")
    team = yaml.safe_load(team_file.read_text(encoding="utf-8")) or {}
    team.setdefault("name", team_dir.name)
    for role in team.get("roles") or []:
        if role.get("prompt") is None:
            name = role.get("name")
            prompt_file = team_dir / "roles" / f"{name}.md"
            if not prompt_file.is_file():
                raise ConfigError(
                    f"role '{name}' in {team_dir} has no inline prompt and no "
                    f"roles/{name}.md")
            role["prompt"] = prompt_file.read_text(encoding="utf-8").strip()
    return team
