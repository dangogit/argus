"""Eval gate: judge parsing (fail-safe), and clean_drafts holding vs promoting a
CI-passing draft based on the rubric score. No live model -- judge is injected."""
import json

from argus.v2.config import loader
from argus.v2.evals import judge as eval_judge
from argus.v2.pm import pending


def test_parse_eval_valid():
    r = eval_judge.parse_eval('ARGUS_EVAL: {"score": 0.9, "notes": "ok", '
                              '"dimensions": {"security": 1.0}}')
    assert r.score == 0.9 and r.dimensions == {"security": 1.0}


def test_parse_eval_garbled_is_failsafe_zero():
    assert eval_judge.parse_eval("the model rambled, no marker").score == 0.0
    assert eval_judge.parse_eval("ARGUS_EVAL: {not json").score == 0.0


def test_score_diff_uses_injected_run():
    r = eval_judge.score_diff("a diff", "a rubric",
                              run=lambda p: 'ARGUS_EVAL: {"score": 0.5}')
    assert r.score == 0.5


def _cfg(tmp_path, threshold):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_path = tmp_path / "argus.yaml"
    pm = f"pm: {{ eval_threshold: {threshold} }}" if threshold is not None else "pm: {}"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        f"    project: {{ repo: {repo}, work_branch_prefix: argus/dev, {pm} }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8")
    return loader.load(cfg_path), repo


def _runner(calls):
    def runner(argv, cwd):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"number": 1, "title": "T", "url": "u1", "isDraft": True,
                                "headRefName": "argus/dev/one", "createdAt": "", "body": ""}])
        if argv[:3] == ["gh", "pr", "checks"]:
            return "true\n"            # CI passes
        if argv[:3] == ["gh", "pr", "diff"]:
            return "diff --git a b"
        return ""
    return runner


def test_gate_disabled_promotes_without_judging(tmp_path):
    cfg, _ = _cfg(tmp_path, None)
    calls = []
    judged = []
    results = pending.clean_drafts(cfg, ["dev"], runner=_runner(calls),
                                   judge=lambda d, r: judged.append(1))
    assert [(r.number, r.action) for r in results] == [(1, "ready")]
    assert ["gh", "pr", "ready", "1"] in calls
    assert judged == []                # judge never called when threshold is None
    assert not any(a[:3] == ["gh", "pr", "diff"] for a in calls)


def test_gate_promotes_on_high_score(tmp_path):
    cfg, _ = _cfg(tmp_path, 0.8)
    calls = []
    results = pending.clean_drafts(cfg, ["dev"], runner=_runner(calls),
                                   judge=lambda d, r: eval_judge.EvalResult(score=0.95))
    assert [(r.number, r.action) for r in results] == [(1, "ready")]
    assert ["gh", "pr", "ready", "1"] in calls


def test_gate_holds_on_low_score(tmp_path):
    cfg, _ = _cfg(tmp_path, 0.8)
    calls = []
    results = pending.clean_drafts(
        cfg, ["dev"], runner=_runner(calls),
        judge=lambda d, r: eval_judge.EvalResult(score=0.4, notes="risky auth change"))
    assert [(r.number, r.action) for r in results] == [(1, "held")]
    assert ["gh", "pr", "ready", "1"] not in calls          # NOT promoted
    assert any(a[:3] == ["gh", "pr", "comment"] and "risky auth change" in a[-1]
               for a in calls)                              # reason surfaced


def test_gate_failsafe_holds_when_judge_raises(tmp_path):
    cfg, _ = _cfg(tmp_path, 0.8)
    calls = []

    def boom(diff, rubric):
        raise RuntimeError("engine down")

    results = pending.clean_drafts(cfg, ["dev"], runner=_runner(calls), judge=boom)
    assert [(r.number, r.action) for r in results] == [(1, "held")]
    assert ["gh", "pr", "ready", "1"] not in calls          # judge failure never promotes
