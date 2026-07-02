"""hermes adapter: a persistent, learning worker per project.

Drives `hermes -z` (oneshot: final response only on stdout, exit 0/1/2) with
HERMES_HOME set to the project's profile, so memory, skills, and session
recall accumulate across cycles. Contract verified against hermes-agent
commit e71d746. Cost is estimated: read back from the profile's state.db when
available (see stats.py), else unpriced-empty.

The hermes engine exists only on the Python engine path; the bash fallback
deliberately has no hermes adapter (unknown engine, exit 3) because a worker
engine with per-project state is post-bash-parity functionality.

ARGUS_PROJECT selects the profile; the PM runner exports it per project before
role calls. ARGUS_PM_ROLE selects the toolset through this Python adapter:
hermes.pm_<role>_toolset wins if set. Otherwise terminal-capable roles
(developer, qa) default to hermes.pm_toolset or file,web,memory,terminal, while
read-only roles (researcher, senior-review) default to hermes.pm_readonly_toolset
or file,web,memory: least privilege, matching the Claude path where only
Developer and QA gained Bash. Non-PM calls keep hermes.toolset, default
file,web,memory.
"""
import os
import shutil
import sys

from argus.config import config_get
from argus.engine import EngineOutageError, EngineResult, write_meta
from argus.engine.adapters._proc import last_stderr, run_with_retries
from argus.hermes import profile, stats
from argus.hermes.settings import hermes_setting


# PM roles that edit or execute and therefore need a terminal-capable toolset.
# Researcher and Senior-review reason about code read-only, so they default to
# no terminal (least privilege, matching the Claude path where only Developer
# and QA gained Bash). Any default is still overridable per role via config.
_TERMINAL_PM_ROLES = {"developer", "qa"}


def _failure_detail() -> str:
    lines = [line.strip() for line in last_stderr().splitlines() if line.strip()]
    return (lines[-1] if lines else "no stderr")[:300]


def _toolsets_for_call() -> str:
    override = os.environ.get("ARGUS_HERMES_TOOLSETS")
    if override:
        return override

    role = (os.environ.get("ARGUS_PM_ROLE") or "").strip().lower()
    if role:
        norm = role.replace("-", "_")
        # Per-role toolset (e.g. hermes.pm_qa_toolset) has no v2 field: it is a
        # narrow per-role escape hatch, kept on the legacy reader only.
        configured = config_get("hermes.pm_" + norm + "_toolset")
        if configured:
            return configured
        if norm in _TERMINAL_PM_ROLES:
            return hermes_setting("pm_toolset", "hermes.pm_toolset") or "file,web,memory,terminal"
        return hermes_setting("pm_readonly_toolset", "hermes.pm_readonly_toolset") or "file,web,memory"

    return hermes_setting("toolset", "hermes.toolset") or "file,web,memory"


def run(prompt: str) -> EngineResult:
    bin_name = os.environ.get("ARGUS_HERMES_BIN", "hermes")
    project = os.environ.get("ARGUS_PROJECT") or "_default"
    toolsets = _toolsets_for_call()
    cwd = os.environ.get("ARGUS_AGENT_CWD") or os.getcwd()

    binpath = shutil.which(bin_name)
    if binpath is None:
        print(f"hermes engine unavailable: {bin_name} not found", file=sys.stderr)
        raise EngineOutageError(bin_name)

    home = profile.ensure_profile(project)
    write_meta("estimated", "")

    argv = [binpath, "-z", prompt, "-t", toolsets]
    out = run_with_retries(argv, cwd=cwd, env={"HERMES_HOME": str(home)})
    if out is None:
        detail = _failure_detail()
        print(f"hermes engine failed: {bin_name} exited non-zero: {detail}", file=sys.stderr)
        raise EngineOutageError(f"{bin_name}: {detail}")

    cost_usd = stats.last_session_cost(home)
    write_meta("estimated", cost_usd or "")
    return EngineResult(text=out.rstrip("\n"), cost_source="estimated", cost_usd=cost_usd or "")
