"""Tests for the browser-verify preview helpers (docs/browser-verify-design.md)."""

from types import SimpleNamespace

import pytest

from argus.v2.browser import (
    BrowserCheckResult,
    PreviewFailed,
    PreviewTimeout,
    build_task,
    diff_touches_ui,
    discover_preview_url,
    parse_verdict,
    run_browser_check,
)
from argus.v2.config import schema
from argus.v2.worker import worker

UI_GLOBS = ["**/*.vue", "src/views/**", "src/components/**", "**/*.css", "src/styles/**"]


def _diff(*paths: str) -> str:
    out = []
    for p in paths:
        out.append(f"diff --git a/{p} b/{p}")
        out.append(f"--- a/{p}")
        out.append(f"+++ b/{p}")
        out.append("@@ -1 +1 @@")
    return "\n".join(out)


def test_diff_touches_ui_true_for_vue_and_component():
    assert diff_touches_ui(_diff("src/components/guarantee-process/Step3.vue"), UI_GLOBS)
    assert diff_touches_ui(_diff("src/views/admin/Foo.vue"), UI_GLOBS)
    assert diff_touches_ui(_diff("src/styles/main.css"), UI_GLOBS)


def test_diff_touches_ui_false_for_backend_only():
    backend = _diff(
        "supabase/functions/credit-check/scoring.ts",
        "src/composables/usePayment.ts",  # a .ts, not UI markup, and not under views/components
    )
    assert not diff_touches_ui(backend, UI_GLOBS)


def test_diff_touches_ui_added_file():
    # New file: git shows --- a/... as /dev/null but +++ b/<real path>.
    d = "diff --git a/src/components/New.vue b/src/components/New.vue\n--- /dev/null\n+++ b/src/components/New.vue\n"
    assert diff_touches_ui(d, UI_GLOBS)


def _http_seq(*responses):
    calls = {"n": 0}

    def http(url, headers):
        assert "Authorization" in headers
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    http.calls = calls
    return http


def test_discover_preview_polls_until_ready():
    http = _http_seq(
        {"deployments": [{"readyState": "BUILDING"}]},
        {"deployments": [{"readyState": "READY", "url": "arvuyot-abc.vercel.app"}]},
    )
    url = discover_preview_url(
        project_id="prj_1", branch="argus/x", token="t",
        poll_interval_seconds=1, build_timeout_seconds=10,
        http=http, sleep=lambda _s: None,
    )
    assert url == "https://arvuyot-abc.vercel.app"
    assert http.calls["n"] == 2


def test_discover_preview_failed_state_raises():
    http = _http_seq({"deployments": [{"readyState": "ERROR"}]})
    with pytest.raises(PreviewFailed):
        discover_preview_url(
            project_id="prj_1", branch="b", token="t",
            http=http, sleep=lambda _s: None,
        )


def test_discover_preview_timeout():
    http = _http_seq({"deployments": [{"readyState": "BUILDING"}]})
    with pytest.raises(PreviewTimeout):
        discover_preview_url(
            project_id="prj_1", branch="b", token="t",
            poll_interval_seconds=1, build_timeout_seconds=2,
            http=http, sleep=lambda _s: None,
        )


def test_discover_preview_requires_config():
    with pytest.raises(PreviewFailed):
        discover_preview_url(project_id="", branch="b", token="t", http=_http_seq({}))
    with pytest.raises(PreviewFailed):
        discover_preview_url(project_id="p", branch="b", token="", http=_http_seq({}))


# --- browser-use runner ------------------------------------------------------

def test_parse_verdict_pass_and_fail():
    assert parse_verdict("PASS looks good").verdict == "pass"
    assert parse_verdict("FAIL button missing").verdict == "fail"
    # verdict on the final line after agent narration
    assert parse_verdict("did stuff\nPASS renders fine").verdict == "pass"


def test_parse_verdict_fail_closed_on_empty_or_ambiguous():
    assert parse_verdict("").verdict == "fail"
    assert parse_verdict("i clicked around and it seemed ok").verdict == "fail"


def test_build_task_includes_url_files_and_login():
    task = build_task(
        url="https://preview.vercel.app/admin",
        changed_files=["src/views/admin/Foo.vue"],
        summary="fix admin table",
        test_login={"phone": "0547505454", "otp": "123456"},
    )
    assert "https://preview.vercel.app/admin" in task
    assert "src/views/admin/Foo.vue" in task
    assert "0547505454" in task and "123456" in task
    assert "PASS" in task and "FAIL" in task


def test_run_browser_check_uses_injected_runner():
    seen = {}

    def fake_runner(*, task, allowed_domains, model, timeout_seconds, browser_venv_python=None):
        seen["task"] = task
        seen["domains"] = allowed_domains
        return "PASS the screen renders"

    res = run_browser_check(
        preview_url="https://preview.vercel.app",
        base_path="/admin",
        changed_files=["src/components/X.vue"],
        summary="tweak",
        allowed_domains=["*.vercel.app"],
        runner=fake_runner,
    )
    assert res.verdict == "pass"
    assert "https://preview.vercel.app/admin" in seen["task"]
    assert seen["domains"] == ["*.vercel.app"]


def test_run_browser_check_fail_closed_on_runner_exception():
    def boom(**_kw):
        raise RuntimeError("chromium crashed")

    res = run_browser_check(
        preview_url="https://p.vercel.app",
        changed_files=["a.vue"],
        summary="x",
        allowed_domains=["*"],
        runner=boom,
    )
    assert res.verdict == "fail"
    assert "chromium crashed" in res.reason


# --- worker._run_browser_verify (gate / push / preview / browser) ------------

def _project(**bv):
    return schema.Project(
        repo="/x",
        browser_verify=schema.BrowserVerify(enabled=True, vercel_project_id="prj", **bv),
    )


def _job():
    return SimpleNamespace(payload={"text": "fix admin table"}, request_id="req-1", role="browser_verify")


def test_bv_worker_skips_backend_only_diff(monkeypatch):
    calls = {"push": 0, "discover": 0}
    monkeypatch.setattr(worker.workspace, "diff", lambda p, w: _diff("src/composables/usePayment.ts"))
    monkeypatch.setattr(worker.workspace, "push", lambda *a, **k: calls.__setitem__("push", calls["push"] + 1))
    monkeypatch.setattr(worker, "discover_preview_url", lambda **k: calls.__setitem__("discover", calls["discover"] + 1) or "https://x")

    _run, result, _actions = worker._run_browser_verify(_job(), _project(), "/wd")
    assert result["parsed"]["verdict"] == "pass"
    assert result["browser_verify"]["skipped"] is True
    assert calls == {"push": 0, "discover": 0}  # no push, no preview poll


def test_bv_worker_ui_diff_runs_browser(monkeypatch):
    seen = {}
    monkeypatch.setattr(worker.workspace, "diff", lambda p, w: _diff("src/components/Admin.vue"))
    monkeypatch.setattr(worker.workspace, "push", lambda project, branch, path: seen.update(branch=branch))
    monkeypatch.setattr(worker, "discover_preview_url", lambda **k: "https://arvuyot-abc.vercel.app")
    monkeypatch.setattr(worker, "run_browser_check", lambda **k: seen.update(k) or BrowserCheckResult("pass", "renders", "PASS renders"))

    _run, result, _actions = worker._run_browser_verify(_job(), _project(api_host="staging.supabase.co"), "/wd")
    assert result["parsed"]["verdict"] == "pass"
    assert result["browser_verify"]["url"] == "https://arvuyot-abc.vercel.app"
    assert "argus/req-1" in seen["branch"]
    assert seen["allowed_domains"] == ["arvuyot-abc.vercel.app", "staging.supabase.co"]
    assert seen["changed_files"] == ["src/components/Admin.vue"]


def test_bv_worker_fail_closed_on_preview_error(monkeypatch):
    monkeypatch.setattr(worker.workspace, "diff", lambda p, w: _diff("src/components/Admin.vue"))
    monkeypatch.setattr(worker.workspace, "push", lambda *a, **k: None)

    def boom(**_k):
        raise PreviewTimeout("no preview after 300s")

    monkeypatch.setattr(worker, "discover_preview_url", boom)
    _run, result, _actions = worker._run_browser_verify(_job(), _project(), "/wd")
    assert result["parsed"]["verdict"] == "fail"
    assert "preview unavailable" in result["parsed"]["analysis"]
