"""Run one job's agent invocation. The engine runs in its OWN subprocess via
the existing argus.engine adapters; this function only builds the prompt from
the job's frozen exec_snapshot and maps the result. One invocation per call =
process-global env in the adapter is safe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import List, Tuple

from argus.engine import EngineOutageError, run_agent
from argus.v2.mcp import config as mcp_config
from argus.v2.queue.models import ActionIntent, Job, RunRecord

_CODE_MODE_CAP = 8000


def build_prompt(job: Job, context: str = "") -> str:
    snap = job.exec_snapshot or {}
    system = snap.get("prompt", "")
    rules = snap.get("rules", "")
    skills = snap.get("skills", "")  # frozen at enqueue; "" when nothing matched
    if rules:
        system = f"{system}\n\n{rules}" if system else rules
    if skills:
        system = f"{system}\n\n{skills}" if system else skills
    text = (job.payload or {}).get("text", "")
    head = f"{context}\n\n" if context else ""
    return f"{head}{system}\n\nTASK:\n{text}".strip()


def run_job(cfg, job: Job, context: str = "",
            workdir: str | None = None) -> Tuple[RunRecord, dict, List[ActionIntent]]:
    snap = job.exec_snapshot or {}
    engine = snap.get("engine", "echo")
    prompt = build_prompt(job, context)
    work = workdir or tempfile.mkdtemp(prefix=f"argus-{job.role}-")

    if engine == "scripted":
        edit = snap.get("scripted_edit")
        if edit:
            p = os.path.join(work, edit["path"])
            os.makedirs(os.path.dirname(p) or work, exist_ok=True)
            with open(p, "w") as f:
                f.write(edit["content"])
        out = snap.get("scripted_output", "")
        run = RunRecord(role=job.role, engine="scripted", status="ok",
                        prompt=prompt, output=out, prompt_hash=snap.get("prompt_hash"))
        return run, {"output": out}, _parse_actions(job, out)

    prev_cwd = os.environ.get("ARGUS_AGENT_CWD")
    prev_project = os.environ.get("ARGUS_PROJECT")
    prev_model = os.environ.get("ARGUS_MODEL")
    prev_network = os.environ.get("ARGUS_CODEX_NETWORK")
    prev_mcp = os.environ.get("ARGUS_CLAUDE_MCP_CONFIG")
    prev_allowed_tools = os.environ.get("ARGUS_CLAUDE_ALLOWED_TOOLS")
    # Render operator-configured MCP servers to a temp file (NOT the worktree -
    # that would pollute the builder's diff) and point the engine at it. echo
    # and other engines ignore the env var; only the MCP-aware engine uses it.
    mcp_dir = None
    mcp_servers = _mcp_servers_for_job(cfg, job)
    if mcp_servers:
        mcp_dir = tempfile.mkdtemp(prefix="argus-mcp-")
        os.environ["ARGUS_CLAUDE_MCP_CONFIG"] = mcp_config.materialize(mcp_servers, mcp_dir)
        allowed = mcp_config.claude_allowed_tools(mcp_servers)
        if allowed:
            os.environ["ARGUS_CLAUDE_ALLOWED_TOOLS"] = ",".join(allowed)
        else:
            os.environ.pop("ARGUS_CLAUDE_ALLOWED_TOOLS", None)
    else:
        os.environ.pop("ARGUS_CLAUDE_MCP_CONFIG", None)
        os.environ.pop("ARGUS_CLAUDE_ALLOWED_TOOLS", None)
    os.environ["ARGUS_AGENT_CWD"] = work
    if snap.get("project"):
        os.environ["ARGUS_PROJECT"] = snap["project"]
    else:
        os.environ.pop("ARGUS_PROJECT", None)
    if snap.get("model"):
        os.environ["ARGUS_MODEL"] = snap["model"]
    else:
        os.environ.pop("ARGUS_MODEL", None)
    # Frozen per-job grant: open network in the (still write-confined) sandbox so
    # the agent can reach GitHub. Only set when the snapshot opted in.
    if snap.get("network"):
        os.environ["ARGUS_CODEX_NETWORK"] = "1"
    else:
        os.environ.pop("ARGUS_CODEX_NETWORK", None)
    try:
        result = run_agent(engine, prompt)
        output_text = result.text
        code_mode = _run_code_mode(result.text, work) if snap.get("allow_code_mode") else None
        if code_mode is not None:
            output_text = (f"{result.text}\n\n[code-mode exit {code_mode['exit']}]\n"
                           f"{code_mode['output']}")
        run = RunRecord(role=job.role, engine=engine, status="ok", prompt=prompt,
                        output=output_text, cost_source=result.cost_source,
                        cost_usd=getattr(result, "cost_usd", None),
                        model=snap.get("model"), prompt_hash=snap.get("prompt_hash"))
        out = {"output": output_text}
        if code_mode is not None:
            out["code_mode"] = code_mode
        actions = _parse_actions(job, result.text)
        return run, out, actions
    except EngineOutageError as e:
        run = RunRecord(role=job.role, engine=engine, status="outage", prompt=prompt,
                        output=str(e), prompt_hash=snap.get("prompt_hash"))
        return run, {"error": str(e)}, []
    finally:
        _restore_env("ARGUS_AGENT_CWD", prev_cwd)
        _restore_env("ARGUS_PROJECT", prev_project)
        _restore_env("ARGUS_MODEL", prev_model)
        _restore_env("ARGUS_CODEX_NETWORK", prev_network)
        _restore_env("ARGUS_CLAUDE_MCP_CONFIG", prev_mcp)
        _restore_env("ARGUS_CLAUDE_ALLOWED_TOOLS", prev_allowed_tools)
        if mcp_dir:
            shutil.rmtree(mcp_dir, ignore_errors=True)


def _mcp_servers_for_job(cfg, job) -> list:
    if cfg is None:
        return []
    by_name = {}
    for server in getattr(getattr(cfg, "mcp", None), "servers", []) or []:
        by_name[server.name] = server
    team_id = getattr(job, "team_id", None)
    if team_id:
        try:
            team = cfg.team(team_id)
        except Exception:
            team = None
        for server in getattr(getattr(team, "mcp", None), "servers", []) or []:
            by_name[server.name] = server
    return list(by_name.values())


def extract_script(text: str):
    """Pull a batched shell script the model emitted under an `ARGUS_SCRIPT:`
    marker, preferring a fenced ```...``` block (falling back to the rest of the
    marker line). Returns None when absent or empty."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("ARGUS_SCRIPT:"):
            rest = lines[i:]
            fence = next((j for j, l in enumerate(rest) if l.strip().startswith("```")), None)
            if fence is None:
                inline = line.split("ARGUS_SCRIPT:", 1)[1].strip()
                return inline or None
            close = next((j for j, l in enumerate(rest[fence + 1:], start=fence + 1)
                          if l.strip().startswith("```")), None)
            if close is None:
                return None
            body = "\n".join(rest[fence + 1:close]).strip()
            return body or None
    return None


def _minimal_env() -> dict:
    """Build a minimal env for the code-mode subprocess. The model-authored
    script is untrusted (prompt injection can steer it), so it must not inherit
    connector secrets (GITHUB_TOKEN, SLACK_BOT_TOKEN, etc.) loaded into the
    worker process from ARGUS_ENV_FILES. Keep only what standard tools and git
    need to run; nothing else crosses the boundary."""
    env = {}
    for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def _run_code_mode(text: str, work: str):
    """Code mode: run ONE model-authored script inside the throwaway worktree to
    batch edits, instead of N action round-trips. The caller gates on the frozen
    snapshot flag (builder/worker role on an allow_code_mode project); untrusted
    advisor/converse paths never set it. Worktree-local only -- outward actions
    still flow through the action pipeline + risk gate. Returns None if no script.

    Runs with a minimal env (see _minimal_env) and a non-login shell (`bash -c`,
    not `-lc`): a login shell sources the user's profile (~/.zshenv,
    ~/.bash_profile, etc.), which can re-import secrets or add unexpected PATH
    entries. Plain `-c` skips that, so the allowlisted env above is the actual
    env the script sees."""
    script = extract_script(text)
    if not script:
        return None
    timeout = float(os.environ.get("ARGUS_CODE_MODE_TIMEOUT", "120"))
    try:
        proc = subprocess.run(["bash", "-c", script], cwd=work, env=_minimal_env(),
                              capture_output=True, text=True, timeout=timeout)
        combined = (proc.stdout or "") + (proc.stderr or "")
        return {"exit": proc.returncode, "output": combined[-_CODE_MODE_CAP:]}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "output": f"code-mode timed out after {timeout}s"}


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def _parse_actions(job: Job, text: str) -> List[ActionIntent]:
    """Agents emit action intents as a JSON line `ARGUS_ACTIONS: [...]`. Absent
    (e.g. echo engine), there are none. Each intent gets a deterministic
    idempotency key derived from the job id + index."""
    marker = "ARGUS_ACTIONS:"
    for line in text.splitlines():
        if line.startswith(marker):
            try:
                items = json.loads(line[len(marker):].strip())
            except json.JSONDecodeError:
                return []
            if not isinstance(items, list):
                return []
            out: List[ActionIntent] = []
            for idx, i in enumerate(items):
                # Skip malformed items (non-dict, or missing a type) instead of
                # raising: a bad model line must never wedge the job into retry.
                if not isinstance(i, dict) or not i.get("type"):
                    continue
                out.append(ActionIntent(
                    type=i["type"],
                    risk=i.get("risk", "irreversible_outward"),  # fail safe
                    idempotency_key=f"{job.id}:{idx}",
                    destination_ref=i.get("destination_ref"),
                    payload=i.get("payload", {}),
                ))
            return out
    return []
