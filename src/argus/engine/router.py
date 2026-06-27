"""Engine selection and outage failover."""
import os
import sys
from typing import Optional

from argus.config import config_get
from argus.engine import EngineOutageError, EngineResult, run_agent


def default_engine(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("ARGUS_ENGINE")
    if env:
        return env
    cfg = config_get("engine.default")
    if cfg:
        return cfg
    return "echo"


def fallback_engine(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    env = os.environ.get("ARGUS_FALLBACK_ENGINE")
    if env:
        return env
    return config_get("engine.fallback")


def run_with_fallback(primary: str, fallback: Optional[str], prompt: str) -> EngineResult:
    """Run the primary adapter; on an outage only, try the fallback once.

    A non-outage failure (unknown engine, real agent error) propagates without
    failover.
    """
    try:
        return run_agent(primary, prompt)
    except EngineOutageError:
        if not fallback or fallback == primary:
            raise
        print(f"argus: {primary} outage, trying fallback {fallback}", file=sys.stderr)
        try:
            return run_agent(fallback, prompt)
        except EngineOutageError:
            print(f"argus: all engines failed ({primary}, {fallback})", file=sys.stderr)
            raise
