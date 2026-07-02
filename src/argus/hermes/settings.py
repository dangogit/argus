"""Resolve hermes engine settings: env > v2 config > legacy argus.config.yaml.

argus.config.py is the old v1 scalar reader (deprecated, still supported for
live installs that have not migrated hermes settings into argus.yaml). New
installs should set `hermes:` on the v2 Config (see argus.v2.config.schema.
HermesConfig and argus.v2.example.yaml); the legacy file stays a fallback so
existing deployments keep working unchanged.
"""
import os
from typing import Optional

from argus.config import config_get

_V2_CACHE_ENV = "_ARGUS_V2_HERMES_CACHE"


def _v2_hermes_value(key: str) -> Optional[str]:
    """Best-effort read of company.hermes.<key> from ARGUS_CONFIG_V2.

    Swallows any load error (missing file, invalid yaml, unrelated schema
    failure) and returns None: a broken or absent v2 config must never break
    hermes settings resolution, only skip this tier.
    """
    path = os.environ.get("ARGUS_CONFIG_V2")
    if not path:
        return None
    try:
        from pathlib import Path

        from argus.v2.config import loader

        cfg = loader.load(Path(path))
    except Exception:
        return None
    hermes = getattr(cfg, "hermes", None)
    if hermes is None:
        return None
    return getattr(hermes, key, None)


def hermes_setting(v2_key: str, legacy_key: str) -> Optional[str]:
    """Resolve one hermes setting: env var > v2 argus.yaml > legacy argus.config.yaml.

    v2_key is the HermesConfig field name (e.g. "toolset"); legacy_key is the
    dotted legacy path (e.g. "hermes.toolset"). No env var is checked here -
    callers that have an existing env override (e.g. ARGUS_HERMES_TOOLSETS)
    check it themselves before falling back to this resolver.
    """
    v2_val = _v2_hermes_value(v2_key)
    if v2_val:
        return v2_val
    return config_get(legacy_key)
