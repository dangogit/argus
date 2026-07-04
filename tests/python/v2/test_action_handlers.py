import json

import pytest

from argus.v2.actions import handlers
from argus.v2.support.apps_script import EmailSummary


def test_open_pr_builds_ready_push_and_gh_commands():
    cmds = handlers.build_open_pr(branch="argus/dev/r1", base="main", remote="origin",
                                  title="Fix login", body="auto")
    assert cmds[0][:3] == ["git", "push", "origin"]
    assert "argus/dev/r1" in cmds[0]
    assert cmds[1][:3] == ["gh", "pr", "create"]
    assert "--draft" not in cmds[1]


def test_open_pr_can_build_draft_pr_command():
    cmds = handlers.build_open_pr(branch="argus/dev/r1", base="main", remote="origin",
                                  title="Fix login", body="auto", draft=True)
    assert "--draft" in cmds[1]


def test_run_handler_uses_injected_runner_and_returns_pr_url():
    calls = []
    def fake_runner(argv, cwd=None):
        calls.append(argv)
        return "https://github.com/x/y/pull/7\n" if argv[:2] == ["gh", "pr"] else ""
    ref = handlers.run("open_pr", {"branch": "b", "base": "main", "remote": "origin",
                                   "title": "t", "body": "x", "cwd": "/tmp"}, runner=fake_runner)
    assert ref == "https://github.com/x/y/pull/7"
    assert len(calls) == 2  # push + pr create


def test_unknown_action_type_raises():
    with pytest.raises(KeyError):
        handlers.run("nope", {}, runner=lambda *a, **k: "")


def test_calendar_action_uses_v2_calendar(monkeypatch):
    from argus.v2 import calendar

    calls = []
    monkeypatch.setattr(
        calendar,
        "run",
        lambda command, payload, json_output=False: calls.append((command, payload, json_output)) or '{"ok":true}',
    )

    ref = handlers.run("calendar_create", {"title": "Call", "start": "2026-06-18T09:00:00Z"})

    assert ref == '{"ok":true}'
    assert calls == [("create", {"title": "Call", "start": "2026-06-18T09:00:00Z"}, True)]


def test_email_search_handler_returns_json_results(monkeypatch):
    class Transport:
        def search(self, query, limit):
            assert query == "from:eesha@example.com"
            assert limit == 3
            return [EmailSummary(thread_id="T9", sender="eesha@example.com",
                                 subject="OpenClaw", snippet="billing")]

    monkeypatch.setattr(handlers, "_email_transport", lambda cfg, team_id: Transport())

    ref = handlers.run("email_search", {"query": "from:eesha@example.com", "limit": 3},
                       cfg=object(), team_id="personal")

    assert json.loads(ref) == [{
        "thread_id": "T9",
        "sender": "eesha@example.com",
        "subject": "OpenClaw",
        "snippet": "billing",
    }]


def test_team_email_action_requires_configured_team_source(monkeypatch, cfg_project):
    monkeypatch.setenv("ARGUS_PERSONAL_GMAIL_APPS_SCRIPT_URL", "https://example.com/app")
    monkeypatch.setenv("ARGUS_PERSONAL_GMAIL_APPS_SCRIPT_KEY", "secret")

    with pytest.raises(RuntimeError, match="team email transport not configured"):
        handlers.run("email_list", {"limit": 1}, cfg=cfg_project, team_id="dev")


def test_social_publish_requires_live_readiness_before_dispatch(monkeypatch):
    monkeypatch.setenv("ARGUS_CONTENT_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("ARGUS_SOCIAL_PUBLISH_COMMAND", "echo posted")
    calls = []

    with pytest.raises(RuntimeError, match="approval proof"):
        handlers.run(
            "social_publish",
            {"project": "luma", "platform": "linkedin"},
            runner=lambda argv, cwd=None: calls.append(argv) or "posted\n",
        )

    assert calls == []


def test_social_publish_runs_after_all_live_readiness_checks(monkeypatch):
    monkeypatch.setenv("ARGUS_CONTENT_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("ARGUS_SOCIAL_PUBLISH_COMMAND", "echo posted")
    calls = []
    readiness = {
        "approval_proof": "approval:123",
        "durable_media": True,
        "cta_routes": True,
        "dm_activation": True,
        "metricool_targets": True,
        "connector_auth": True,
    }

    ref = handlers.run(
        "social_publish",
        {"project": "luma", "platform": "linkedin", "live_readiness": readiness},
        runner=lambda argv, cwd=None: calls.append(argv) or "posted\n",
    )

    assert ref == "posted"
    assert calls == [["bash", "-lc", "echo posted"]]


# ---------------------------------------------------------------------------
# New PM action handlers: close_pr, comment_pr, reopen_pr
# ---------------------------------------------------------------------------

def _fake_runner(responses=None):
    """Returns a runner that records calls and returns canned responses."""
    calls = []
    responses = responses or {}

    def runner(argv, cwd=None):
        calls.append(argv)
        key = tuple(argv[:4])
        return responses.get(key, f"ok:{':'.join(argv[:4])}\n")

    runner.calls = calls
    return runner


def test_close_pr_calls_gh_pr_close_and_returns_ref():
    runner = _fake_runner()
    ref = handlers.run("close_pr", {"number": 73, "repo": "o/r"}, runner=runner)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:3] == ["gh", "pr", "close"]
    assert "73" in argv
    assert "-R" in argv and "o/r" in argv
    assert ref  # returns a non-empty provider_ref


def test_comment_pr_calls_gh_pr_comment_with_body_and_returns_ref():
    runner = _fake_runner()
    ref = handlers.run("comment_pr",
                       {"number": 73, "repo": "o/r", "body": "LGTM"},
                       runner=runner)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:3] == ["gh", "pr", "comment"]
    assert "73" in argv
    assert "-R" in argv and "o/r" in argv
    # body must be passed as the --body flag value
    body_idx = argv.index("--body")
    assert argv[body_idx + 1] == "LGTM"
    assert ref


def test_reopen_pr_calls_gh_pr_reopen_and_returns_ref():
    runner = _fake_runner()
    ref = handlers.run("reopen_pr", {"number": 73, "repo": "o/r"}, runner=runner)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:3] == ["gh", "pr", "reopen"]
    assert "73" in argv
    assert "-R" in argv and "o/r" in argv
    assert ref


# ---------------------------------------------------------------------------
# risk_for: server-side risk classification
# ---------------------------------------------------------------------------

def test_risk_for_reversible_types():
    from argus.v2.actions import executor
    for t in ("open_pr", "close_pr", "comment_pr", "reopen_pr", "remember",
              "reply", "notify", "calendar_list", "email_list", "email_search",
              "email_read", "email_draft"):
        assert executor.risk_for(t) == "reversible_internal", f"Expected reversible for {t}"


def test_risk_for_personal_outward_types():
    from argus.v2.actions import executor
    for t in ("calendar_create", "calendar_update", "calendar_delete",
              "email_reply", "email_archive", "content_queue", "social_publish"):
        assert executor.risk_for(t) == "personal_outward", f"Expected personal_outward for {t}"


def test_risk_for_irreversible_types():
    from argus.v2.actions import executor
    for t in ("merge_pr", "deploy"):
        assert executor.risk_for(t) == "irreversible_outward", f"Expected irreversible for {t}"


def test_risk_for_unknown_type_fails_safe():
    from argus.v2.actions import executor
    # Unknown types default to irreversible_outward (fail safe).
    assert executor.risk_for("rm") == "irreversible_outward"
    assert executor.risk_for("unknown_op") == "irreversible_outward"
