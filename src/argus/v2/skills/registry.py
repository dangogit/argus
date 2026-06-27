"""Load-on-relevance skills: markdown playbooks injected into a role/agent
prompt only when the incoming text matches their triggers. Keeps prompts
bounded -- a no-match adds zero tokens (Effective Context Engineering: find the
smallest high-signal token set; every always-on token competes with conversation
history and is paid even when irrelevant).

A skill file is markdown with a small YAML frontmatter block:

    ---
    name: pr-review
    triggers: [review, diff, lint, pr]
    roles: [judge, senior]   # optional; empty => any role
    ---
    <body injected into the prompt when selected>

Selection is deterministic (keyword/trigger match, bounded to max_k), so a job
replays identically -- the worker path freezes the rendered block into the job's
exec_snapshot at enqueue time alongside the role prompt. Files whose name starts
with `_` are skipped (docs, e.g. `_README.md`), so they never become a skill.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    triggers: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()      # empty => applies to any role
    body: str = ""


def parse_skill(text: str, *, fallback_name: str) -> Skill:
    m = _FRONTMATTER.match(text.lstrip("﻿"))
    if not m:
        return Skill(name=fallback_name, body=text.strip())
    meta = yaml.safe_load(m.group(1)) or {}
    return Skill(
        name=str(meta.get("name") or fallback_name),
        triggers=tuple(str(t).lower() for t in (meta.get("triggers") or [])),
        roles=tuple(str(r) for r in (meta.get("roles") or [])),
        body=m.group(2).strip(),
    )


def load_skills(*dirs) -> dict[str, Skill]:
    """Load every `*.md` (except `_`-prefixed) under the given dirs. Later dirs
    override earlier ones by skill name, so a per-team dir shadows a bundled one."""
    out: dict[str, Skill] = {}
    for d in dirs:
        if not d:
            continue
        p = Path(d).expanduser()
        if not p.is_dir():
            continue
        for f in sorted(p.glob("*.md")):
            if f.name.startswith("_"):
                continue
            skill = parse_skill(f.read_text(encoding="utf-8"), fallback_name=f.stem)
            out[skill.name] = skill
    return out


def select(skills, *, role: str, text: str, allow=None, max_k: int = 2) -> list[Skill]:
    """Pick up to max_k skills relevant to `text` for `role`.

    - allow: if given (the role's declared skill list), only those names are
      candidates; None => every loaded skill is a candidate.
    - A skill is eligible if its `roles` is empty or contains `role`.
    - Score = count of distinct triggers present in the lowercased text; only
      score > 0 is returned, highest first, ties broken by name (determinism)."""
    if isinstance(skills, dict):
        skills = list(skills.values())
    allow_set = set(allow) if allow is not None else None
    low = (text or "").lower()
    scored = []
    for s in skills:
        if allow_set is not None and s.name not in allow_set:
            continue
        if s.roles and role not in s.roles:
            continue
        hits = sum(1 for t in s.triggers if t and t in low)
        if hits:
            scored.append((hits, s.name, s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, _, s in scored[:max_k]]


def render(selected) -> str:
    """Render selected skills as a prompt block, or "" for an empty selection
    (callers append unconditionally, so a no-match contributes nothing)."""
    bodies = "\n\n".join(s.body for s in selected if s.body)
    return f"SKILLS (apply when relevant):\n\n{bodies}" if bodies else ""


def skill_dirs(extra=None) -> list:
    """Resolution order (later shadows earlier): bundled `prompts/skills/`, then
    `$ARGUS_SKILLS_DIR`, then an explicit per-team dir. Mirrors the advisor's
    `ARGUS_ADVISOR_CONTRACT_DIR` override precedent."""
    dirs: list = [Path(__file__).resolve().parents[4] / "prompts" / "skills"]
    env = os.environ.get("ARGUS_SKILLS_DIR")
    if env:
        dirs.append(env)
    if extra:
        dirs.append(extra)
    return dirs


def block_for(role: str, text: str, *, dirs=None, allow=None, max_k: int = 2) -> str:
    """One-shot helper for inline callers (advisor, assistant): load -> select ->
    render. Returns "" when nothing matches."""
    skills = load_skills(*(dirs if dirs is not None else skill_dirs()))
    return render(select(skills, role=role, text=text, allow=allow, max_k=max_k))
