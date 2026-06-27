"""Load + validate argus.yaml; resolve secrets and the engine cascade."""
from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path

import yaml

from argus.v2.config.schema import Config, EngineSpec

_SECRET = re.compile(r"^\$\{(env|keychain):([^}]+)\}$")
_SUPPORT_SOURCE_TYPES = {"support_apps_script", "apps_script_support"}
_CONFIG_ONLY_CHANNEL_TYPES = {"cli"}


class ConfigError(Exception):
    pass


def _resolve_secret(ref: str | None) -> str | None:
    if not ref:
        return None
    m = _SECRET.match(ref)
    if not m:
        raise ConfigError(f"bad secret ref: {ref!r}")
    kind, name = m.group(1), m.group(2)
    if kind == "env":
        val = os.environ.get(name)
        if val is None:
            raise ConfigError(f"unresolved env secret: {name}")
        return val
    # keychain resolution is wired with the wizard; not in slice 1.
    raise ConfigError(f"secret backend not available in slice 1: {kind}")


def _resolve_config_value(value):
    if isinstance(value, str):
        m = _SECRET.match(value)
        if not m:
            return value
        kind, name = m.group(1), m.group(2)
        if kind != "env":
            raise ConfigError(f"config backend not available in slice 1: {kind}")
        val = os.environ.get(name)
        if val is None:
            raise ConfigError(f"unresolved env config: {name}")
        return val
    if isinstance(value, list):
        return [_resolve_config_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_config_value(v) for k, v in value.items()}
    return value


def _check_autonomy(autonomy, where: str) -> None:
    """Reject any autonomy policy that makes irreversible/outward actions auto."""
    if autonomy is None:
        return
    if getattr(autonomy, "irreversible_outward", None) == "auto":
        raise ConfigError(
            f"{where}: autonomy.irreversible_outward must not be 'auto' "
            f"(irreversible or outward actions always require approval)")


def _normalize_raw(raw: dict) -> dict:
    defaults = ((raw.get("company") or {}).get("defaults") or {})
    pipeline_default = defaults.get("pipeline")
    for team in raw.get("teams") or []:
        if team.get("pipeline") is None:
            if pipeline_default is None:
                name = team.get("name", "<unnamed>")
                raise ConfigError(
                    f"team {name} missing pipeline and company.defaults.pipeline is not set")
            team["pipeline"] = deepcopy(pipeline_default)
    return raw


def _apply_project_defaults(cfg: Config) -> None:
    defaults = cfg.company.defaults.project
    for team in cfg.teams:
        project = team.project
        if project is None:
            continue
        for field in defaults.model_fields_set - {"autofix", "pm"}:
            if field in project.model_fields_set:
                continue
            value = getattr(defaults, field)
            if value is not None:
                setattr(project, field, value)
        for field in defaults.autofix.model_fields_set:
            if field not in project.autofix.model_fields_set:
                value = getattr(defaults.autofix, field)
                if value is not None:
                    setattr(project.autofix, field, value)
        for field in defaults.pm.model_fields_set:
            if field not in project.pm.model_fields_set:
                value = getattr(defaults.pm, field)
                if value is not None:
                    setattr(project.pm, field, value)


def _merge_source_config_defaults(cfg: Config, source) -> None:
    if source.type in _SUPPORT_SOURCE_TYPES and cfg.company.defaults.support:
        source.config = {**cfg.company.defaults.support, **(source.config or {})}


def _supported_channel_types() -> set[str]:
    import argus.v2.channels  # noqa: F401
    from argus.v2.channels.base import REGISTRY

    return set(REGISTRY) | _CONFIG_ONLY_CHANNEL_TYPES


def _check_channel_types(cfg: Config) -> None:
    supported = _supported_channel_types()
    for team in cfg.teams:
        for channel in team.channels:
            if channel.type not in supported:
                names = ", ".join(sorted(supported))
                raise ConfigError(
                    f"team {team.name}: unsupported channel type {channel.type!r} "
                    f"(supported: {names})"
                )


def load(path: Path) -> Config:
    p = Path(path)
    if p.is_dir():
        # Agent-as-directory form; compiles to the same dict as the YAML file.
        from argus.v2.config import dir_loader
        raw = dir_loader.compile(p)
    else:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    raw = _normalize_raw(raw or {})
    cfg = Config.model_validate(raw)
    _apply_project_defaults(cfg)
    _check_channel_types(cfg)
    # webhook_secret may be a literal or a ${env:...} ref like every other secret.
    if cfg.company.defaults.webhook_secret:
        cfg.company.defaults.webhook_secret = _resolve_config_value(
            cfg.company.defaults.webhook_secret)
    cfg.company.defaults.support = _resolve_config_value(cfg.company.defaults.support)
    for s in cfg.company.sources:
        _merge_source_config_defaults(cfg, s)
        s.secret = _resolve_secret(s.secret_ref)
        s.config = _resolve_config_value(s.config)
    # Safety invariant: irreversible/outward actions (merge to base, deploy,
    # customer sends) must NEVER be auto. Hard-block any config that tries, so a
    # typo cannot defeat the approval gate.
    _check_autonomy(cfg.company.defaults.autonomy, "company.defaults")
    for t in cfg.teams:
        if t.project and t.project.pm.max_rework_attempts is not None:
            t.pipeline.max_iters = t.project.pm.max_rework_attempts
        _check_autonomy(getattr(t, "autonomy", None), f"team {t.name}")
        for s in t.sources:
            _merge_source_config_defaults(cfg, s)
            s.secret = _resolve_secret(s.secret_ref)
            s.config = _resolve_config_value(s.config)
        for ch in t.channels:
            ch.secret = _resolve_secret(ch.secret_ref)
            ch.config = _resolve_config_value(ch.config)
        for r in t.roles:
            eng = resolve_engine(cfg, t.name, r.name)
            if r.vision and eng.engine == "hermes":
                raise ConfigError(
                    f"role {t.name}.{r.name} has vision=true but resolves to hermes "
                    f"(hermes ignores images); pin an image-capable engine"
                )
    return cfg


def resolve_engine(cfg: Config, team_name: str, role_name: str) -> EngineSpec:
    team = cfg.team(team_name)
    role = team.role(role_name)
    if role.engine:
        return role.engine
    if team.engine:
        return team.engine
    if cfg.company.defaults.engine:
        return cfg.company.defaults.engine
    return EngineSpec(engine="echo")
