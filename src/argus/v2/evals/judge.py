"""LLM-as-judge scoring of a PR diff against a rubric, for the draft->ready gate.

This is the MODEL-based grader: it judges the artifact (the diff itself, not the
steps that produced it) on dimensions code-based checks can't -- code quality,
security reasoning, spec adherence. CI (`gh pr checks`) is the code-based grader
and runs first; the judge only sees diffs that already pass CI.

Design (Demystifying Evals): grade the artifact not the steps; partial credit on
a 0..1 continuum; let the judge answer per-dimension; give it an escape so it
never invents a score. Parsing is fail-safe: a garbled or missing verdict scores
0.0, so a broken judge HOLDS the PR as draft rather than auto-promoting it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

_MARK = "ARGUS_EVAL:"
_DIFF_CAP = 20000  # keep judge prompts bounded; truncation is flagged in-prompt


@dataclass(frozen=True)
class EvalResult:
    score: float                         # 0..1 overall
    notes: str = ""
    dimensions: dict = field(default_factory=dict)


def build_prompt(diff: str, rubric: str) -> str:
    diff = diff or ""
    if len(diff) > _DIFF_CAP:
        diff = diff[:_DIFF_CAP] + "\n...[diff truncated; judge on what is shown]..."
    return (
        "You are a strict, independent code reviewer scoring a pull request diff "
        "against the rubric below. Judge the DIFF ITSELF, not how it was produced.\n\n"
        "Score each rubric dimension from 0.0 to 1.0 (partial credit is expected). "
        "If the diff gives no evidence for a dimension, score it null (\"N/A\") "
        "rather than guessing. The overall score is the mean of the dimensions you "
        "could judge.\n\n"
        "Output exactly one line, nothing after it:\n"
        "ARGUS_EVAL: {\"score\": <0..1>, \"dimensions\": {\"<name>\": <0..1|null>, ...}, "
        "\"notes\": \"<=2 sentences, name the biggest risk\"}\n\n"
        f"RUBRIC:\n{rubric}\n\nDIFF:\n{diff}"
    )


def parse_eval(text: str) -> EvalResult:
    for line in (text or "").splitlines():
        if line.startswith(_MARK):
            try:
                v = json.loads(line[len(_MARK):].strip())
            except json.JSONDecodeError:
                break
            if not isinstance(v, dict):
                break
            try:
                score = float(v.get("score"))
            except (TypeError, ValueError):
                break
            dims = v.get("dimensions") if isinstance(v.get("dimensions"), dict) else {}
            return EvalResult(score=max(0.0, min(1.0, score)),
                              notes=str(v.get("notes") or ""), dimensions=dims)
    # fail-safe: no parseable verdict => 0.0 => the gate holds the draft
    return EvalResult(score=0.0, notes="eval: no parseable verdict (held as draft)")


def score_diff(diff: str, rubric: str, *, run: Callable[[str], str]) -> EvalResult:
    """`run` maps a prompt to the model's text output (engine-agnostic, injectable
    so tests need no live model)."""
    return parse_eval(run(build_prompt(diff, rubric)))
