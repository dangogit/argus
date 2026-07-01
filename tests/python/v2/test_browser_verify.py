"""Tests for the browser-verify preview helpers (docs/browser-verify-design.md)."""

import pytest

from argus.v2.browser import (
    PreviewFailed,
    PreviewTimeout,
    diff_touches_ui,
    discover_preview_url,
)

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
