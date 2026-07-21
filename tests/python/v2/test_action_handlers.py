import json

import pytest

from argus.v2.actions import handlers
from argus.v2.config import loader
from argus.v2.ownership.github import PullRequestState
from argus.v2.ownership import support as ownership_support
from argus.v2.support.apps_script import EmailSummary
from argus.v2.support.cycle import DraftDecision


SHA40 = "a" * 40
OTHER_SHA40 = "b" * 40
SHA64 = "c" * 64


@pytest.fixture()
def cfg_ownership(tmp_path):
    path = tmp_path / "ownership-actions.yaml"
    path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    autonomy:\n"
        "      actions: { ready_pr: auto, merge_pr: auto }\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      code:\n"
        "        auto_ready: true\n"
        "        auto_merge: true\n"
        "        allowed_base_branches: [staging]\n"
        "        required_checks: [test]\n"
        "    project:\n"
        "      repo: /repo\n"
        "      github_repo: acme/luma\n"
        "      work_branch_prefix: argus\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    return loader.load(path)


@pytest.fixture()
def cfg_support_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPPORT_KEY", "test-key")
    path = tmp_path / "support-actions.yaml"
    path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - { type: support_apps_script, name: luma-mail, team: luma, "
        "secret_ref: '${env:SUPPORT_KEY}', config: { url: 'https://support.test' } }\n"
        "teams:\n  - name: luma\n"
        "    autonomy: { actions: { support_reply: auto } }\n"
        "    ownership:\n"
        "      enabled: true\n"
        "      support: { auto_send_low_risk: true, min_confidence: 0.92 }\n"
        "    roles: [ { name: support, kind: worker, prompt: p } ]\n"
        "    pipeline: { stages: [support] }\n",
        encoding="utf-8",
    )
    return loader.load(path)


def _queued_support_action(conn, cfg_support_ownership, *, thread="T-handler"):
    team = cfg_support_ownership.team("luma")
    source = cfg_support_ownership.company.sources[0]
    decision = DraftDecision(
        reply="Open Settings and choose Export.", category="how_to",
        risk="low", confidence=0.96,
    )
    obligation = ownership_support.open_or_update_obligation(
        conn, team=team, source=source, thread_id=thread,
        sender="user@example.com", subject="Export",
        raw_thread="How do I export?", decision=decision,
    )
    action_id, _inserted = ownership_support.queue_reply_action(
        conn, team=team, source=source, obligation=obligation,
        decision=decision,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM actions WHERE id=%s", (action_id,))
        payload = cur.fetchone()[0]
    conn.commit()
    return obligation, payload


def test_support_reply_handler_reloads_state_sends_once_and_closes_obligation(
        conn, cfg_support_ownership, monkeypatch):
    obligation, payload = _queued_support_action(conn, cfg_support_ownership)
    calls = []

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def reply(self, thread_id, body):
            calls.append(("reply", thread_id, body))

        def mark_read(self, thread_id):
            calls.append(("read", thread_id))

        def archive(self, thread_id):
            calls.append(("archive", thread_id))

    monkeypatch.setattr(ownership_support, "AppsScriptTransport", Transport)

    ref = handlers.run(
        "support_reply", payload, cfg=cfg_support_ownership,
        team_id="luma", conn=conn,
    )
    conn.commit()

    assert ref == "support:T-handler"
    assert calls == [
        ("reply", "T-handler", "Open Settings and choose Export."),
        ("read", "T-handler"),
        ("archive", "T-handler"),
    ]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, provider_ref FROM team_obligations WHERE id=%s",
            (obligation.id,),
        )
        assert cur.fetchone() == ("done", "support:T-handler")


def test_support_reply_handler_revalidates_persisted_sensitive_thread(
        conn, cfg_support_ownership, monkeypatch):
    obligation, payload = _queued_support_action(conn, cfg_support_ownership)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE team_obligations SET evidence=evidence || "
            "'{\"raw_thread\":\"Please refund me\"}'::jsonb WHERE id=%s",
            (obligation.id,),
        )
    conn.commit()
    monkeypatch.setattr(
        ownership_support, "AppsScriptTransport",
        lambda **_: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    with pytest.raises(RuntimeError, match="support policy denied"):
        handlers.run(
            "support_reply", payload,
            cfg=cfg_support_ownership, team_id="luma", conn=conn,
        )

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM team_obligations WHERE id=%s", (obligation.id,))
        assert cur.fetchone()[0] == "blocked"


def test_support_reply_handler_rejects_payload_identity_tampering(
        conn, cfg_support_ownership, monkeypatch):
    _obligation, payload = _queued_support_action(conn, cfg_support_ownership)
    monkeypatch.setattr(
        ownership_support, "AppsScriptTransport",
        lambda **_: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    with pytest.raises(RuntimeError, match="persisted state"):
        handlers.run(
            "support_reply", {**payload, "thread_id": "attacker-thread"},
            cfg=cfg_support_ownership, team_id="luma", conn=conn,
        )


def test_support_reply_ambiguous_failure_is_not_retried(
        conn, cfg_support_ownership, monkeypatch):
    obligation, payload = _queued_support_action(conn, cfg_support_ownership)
    calls = []

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def reply(self, thread_id, body):
            calls.append((thread_id, body))
            raise RuntimeError("connection dropped after send")

        def mark_read(self, _thread_id):
            raise AssertionError("must not mark read")

        def archive(self, _thread_id):
            raise AssertionError("must not archive")

    monkeypatch.setattr(ownership_support, "AppsScriptTransport", Transport)

    with pytest.raises(RuntimeError, match="connection dropped after send"):
        handlers.run(
            "support_reply", payload, cfg=cfg_support_ownership,
            team_id="luma", conn=conn,
        )
    conn.commit()
    with pytest.raises(RuntimeError, match="delivery outcome is uncertain"):
        handlers.run(
            "support_reply", payload, cfg=cfg_support_ownership,
            team_id="luma", conn=conn,
        )

    assert calls == [("T-handler", "Open Settings and choose Export.")]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, evidence ? 'delivery_attempted_at' "
            "FROM team_obligations WHERE id=%s", (obligation.id,),
        )
        assert cur.fetchone() == ("blocked", True)


@pytest.fixture()
def fake_runner():
    class Runner:
        def __init__(self):
            self.calls = []
            self.common_dirs = {
                "/repo": "/repo/.git",
                "/repo-worktree": "/repo/.git",
                "/foreign": "/foreign/.git",
            }
            self.remote_url = "git@github.com:acme/luma.git"

        def __call__(self, argv, cwd=None):
            self.calls.append((argv, cwd))
            if argv == [
                "git", "rev-parse", "--path-format=absolute", "--git-common-dir",
            ]:
                if cwd not in self.common_dirs:
                    raise RuntimeError("not a git checkout")
                return f"{self.common_dirs[cwd]}\n"
            if argv[:3] == ["git", "remote", "get-url"]:
                return f"{self.remote_url}\n"
            return "ok\n"

        @property
        def mutations(self):
            return [
                call for call in self.calls
                if call[0][:3] in (
                    ["gh", "pr", "ready"],
                    ["gh", "pr", "merge"],
                )
            ]

    return Runner()


def _owned_pr(*, number=42, url=None, state="OPEN", draft=True, clean=True,
              checks_passed=True, base="staging", head="argus/req-1",
              head_sha=SHA40, files=None):
    return PullRequestState(
        number=number,
        url=url or f"https://github.com/acme/luma/pull/{number}",
        state=state,
        draft=draft,
        clean=clean,
        base=base,
        head=head,
        head_sha=head_sha,
        changed_files=tuple(files if files is not None else ["src/App.tsx"]),
        checks=("test",),
        checks_passed=checks_passed,
    )


def test_ready_pr_command(fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: _owned_pr())

    handlers.run(
        "ready_pr",
        {"pr": "42", "cwd": "/repo", "expected_head_sha": SHA40},
        runner=fake_runner,
        cfg=cfg_ownership,
        team_id="dev",
    )

    assert fake_runner.mutations == [
        (["gh", "pr", "ready", "42"], "/repo")
    ]


def test_ready_pr_requires_open_pr(fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "inspect_pr",
        lambda **_: _owned_pr(state="CLOSED", draft=True, clean=False),
    )

    with pytest.raises(RuntimeError, match="blocked by ownership policy"):
        handlers.run(
            "ready_pr",
            {"pr": "42", "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_merge_pr_command(fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers, "inspect_pr", lambda **_: _owned_pr(draft=False))

    handlers.run(
        "merge_pr",
        {"pr": "42", "cwd": "/repo", "expected_head_sha": SHA40},
        runner=fake_runner,
        cfg=cfg_ownership,
        team_id="dev",
    )

    assert fake_runner.mutations == [
        (["gh", "pr", "merge", "42", "--squash", "--delete-branch"], "/repo")
    ]


def test_merge_pr_rechecks_policy_before_command(
        fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers, "inspect_pr", lambda **_: _owned_pr(clean=False))

    with pytest.raises(RuntimeError, match="blocked by ownership policy"):
        handlers.run(
            "merge_pr",
            {"pr": "42", "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_merge_pr_requires_non_draft(fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: _owned_pr())

    with pytest.raises(RuntimeError, match="non-draft"):
        handlers.run(
            "merge_pr",
            {"pr": "42", "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


@pytest.mark.parametrize("pr_ref", [
    "https://github.com/acme/luma/pull/42",
    "argus/req-1",
    "--repo",
    " 42",
    "42 ",
    "01",
    "0",
    0,
    -1,
    True,
])
def test_ownership_actions_reject_noncanonical_pr_refs(
        pr_ref, fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: _owned_pr())

    with pytest.raises(RuntimeError, match="positive canonical PR number"):
        handlers.run(
            "ready_pr",
            {"pr": pr_ref, "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


@pytest.mark.parametrize("expected_head_sha", [
    "", "a" * 39, "a" * 41, "g" * 40, f" {SHA40}", 42,
])
def test_ownership_actions_require_full_expected_head_sha(
        expected_head_sha, fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: _owned_pr())

    with pytest.raises(RuntimeError, match="expected_head_sha"):
        handlers.run(
            "ready_pr",
            {"pr": 42, "cwd": "/repo", "expected_head_sha": expected_head_sha},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_ownership_action_rejects_foreign_checkout(
        fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: _owned_pr())

    with pytest.raises(RuntimeError, match="configured project checkout"):
        handlers.run(
            "ready_pr",
            {"pr": 42, "cwd": "/foreign", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_ownership_action_rejects_unprovable_checkout(
        fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: _owned_pr())

    with pytest.raises(RuntimeError, match="cannot verify ownership action cwd"):
        handlers.run(
            "ready_pr",
            {"pr": 42, "cwd": "/missing", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


@pytest.mark.parametrize("cwd", ["/repo", "/repo-worktree"])
def test_ownership_action_accepts_configured_repo_or_linked_worktree(
        cwd, fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: _owned_pr())

    handlers.run(
        "ready_pr",
        {"pr": 42, "cwd": cwd, "expected_head_sha": SHA40},
        runner=fake_runner,
        cfg=cfg_ownership,
        team_id="dev",
    )

    assert fake_runner.mutations == [
        (["gh", "pr", "ready", "42"], cwd)
    ]


def test_ownership_action_rejects_foreign_repository_url(
        fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "inspect_pr",
        lambda **_: _owned_pr(url="https://github.com/acme/other/pull/42"),
    )

    with pytest.raises(RuntimeError, match="configured GitHub repository"):
        handlers.run(
            "ready_pr",
            {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_ownership_action_rejects_inspected_number_mismatch(
        fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers, "inspect_pr", lambda **_: _owned_pr(number=43))

    with pytest.raises(RuntimeError, match="inspected PR number does not match"):
        handlers.run(
            "ready_pr",
            {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_ownership_action_derives_enterprise_repo_from_configured_remote(
        fake_runner, cfg_ownership, monkeypatch):
    cfg_ownership.team("dev").project.github_repo = None
    fake_runner.remote_url = "git@ghe.example.com:acme/luma.git"
    monkeypatch.setattr(
        handlers,
        "inspect_pr",
        lambda **_: _owned_pr(url="https://ghe.example.com/acme/luma/pull/42"),
    )

    handlers.run(
        "ready_pr",
        {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA40},
        runner=fake_runner,
        cfg=cfg_ownership,
        team_id="dev",
    )

    assert fake_runner.mutations == [
        (["gh", "pr", "ready", "42"], "/repo")
    ]


def test_ready_pr_reconciles_external_success_without_second_mutation(
        fake_runner, cfg_ownership, monkeypatch):
    states = iter([_owned_pr(draft=True), _owned_pr(draft=False)])
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: next(states))
    payload = {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA40}

    first_ref = handlers.run(
        "ready_pr", payload, runner=fake_runner,
        cfg=cfg_ownership, team_id="dev")
    retry_ref = handlers.run(
        "ready_pr", payload, runner=fake_runner,
        cfg=cfg_ownership, team_id="dev")

    assert retry_ref == first_ref
    assert fake_runner.mutations == [
        (["gh", "pr", "ready", "42"], "/repo")
    ]


def test_merge_pr_reconciles_external_success_without_second_mutation(
        fake_runner, cfg_ownership, monkeypatch):
    states = iter([
        _owned_pr(draft=False),
        _owned_pr(state="MERGED", draft=False, clean=False),
    ])
    monkeypatch.setattr(handlers, "inspect_pr", lambda **_: next(states))
    payload = {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA40}

    first_ref = handlers.run(
        "merge_pr", payload, runner=fake_runner,
        cfg=cfg_ownership, team_id="dev")
    retry_ref = handlers.run(
        "merge_pr", payload, runner=fake_runner,
        cfg=cfg_ownership, team_id="dev")

    assert retry_ref == first_ref
    assert fake_runner.mutations == [
        (["gh", "pr", "merge", "42", "--squash", "--delete-branch"], "/repo")
    ]


@pytest.mark.parametrize("action_type", ["ready_pr", "merge_pr"])
def test_ownership_action_rejects_live_head_mismatch(
        action_type, fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers, "inspect_pr", lambda **_: _owned_pr(head_sha=OTHER_SHA40))

    with pytest.raises(RuntimeError, match="head SHA does not match"):
        handlers.run(
            action_type,
            {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_merge_pr_rejects_closed_unmerged_retry(
        fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "inspect_pr",
        lambda **_: _owned_pr(state="CLOSED", draft=False, clean=False),
    )

    with pytest.raises(RuntimeError, match="closed without merge"):
        handlers.run(
            "merge_pr",
            {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA40},
            runner=fake_runner,
            cfg=cfg_ownership,
            team_id="dev",
        )

    assert fake_runner.mutations == []


def test_ownership_action_accepts_sha256_expected_head(
        fake_runner, cfg_ownership, monkeypatch):
    monkeypatch.setattr(
        handlers, "inspect_pr", lambda **_: _owned_pr(head_sha=SHA64))

    handlers.run(
        "ready_pr",
        {"pr": 42, "cwd": "/repo", "expected_head_sha": SHA64},
        runner=fake_runner,
        cfg=cfg_ownership,
        team_id="dev",
    )

    assert fake_runner.mutations == [
        (["gh", "pr", "ready", "42"], "/repo")
    ]


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
        return "https://github.com/x/y/pull/7\n" if argv[:3] == ["gh", "pr", "create"] else "[]"
    ref = handlers.run("open_pr", {"branch": "b", "base": "main", "remote": "origin",
                                   "title": "t", "body": "x", "cwd": "/tmp"}, runner=fake_runner)
    assert ref == "https://github.com/x/y/pull/7"
    assert len(calls) == 3  # duplicate scan + push + pr create


def test_open_pr_adds_dedupe_signature_to_body(monkeypatch, tmp_path):
    monkeypatch.setattr(handlers.mergeability, "check", lambda cwd, base, remote:
                        handlers.mergeability.MergeCheck(True, False, False))
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        return "https://github.com/x/y/pull/7\n" if argv[:3] == ["gh", "pr", "create"] else ""

    handlers.run("open_pr", {
        "branch": "argus/dev/r1",
        "base": "main",
        "remote": "origin",
        "title": "Fix login",
        "body": "Body",
        "request": "Login fails",
        "summary_short": "Fixed login",
        "changed_files": ["src/auth.py"],
        "cwd": str(tmp_path),
    }, runner=runner, team_id="dev")

    create_cmd = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    body = create_cmd[create_cmd.index("--body") + 1]
    assert "Body" in body
    assert "argus-pr-signature:" in body


def test_open_pr_reuses_existing_pr_with_same_signature(tmp_path):
    payload = {
        "branch": "argus/dev/r1",
        "base": "main",
        "remote": "origin",
        "title": "Fix login",
        "body": "Body",
        "request": "Login fails",
        "summary_short": "Fixed login",
        "changed_files": ["src/auth.py"],
        "cwd": str(tmp_path),
    }
    signature = handlers._pr_signature(payload, team_id="dev")
    calls = []

    def runner(argv, cwd=None):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([{
                "number": 12,
                "url": "https://github.com/x/y/pull/12",
                "title": "Fix login",
                "body": f"<!-- argus-pr-signature:{signature} -->",
                "headRefName": "argus/dev/other",
            }])
        return ""

    ref = handlers.run("open_pr", payload, runner=runner, team_id="dev")

    assert ref == "https://github.com/x/y/pull/12"
    assert any(cmd[:3] == ["gh", "pr", "comment"] for cmd in calls)
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_unknown_action_type_raises():
    with pytest.raises(KeyError):
        handlers.run("nope", {}, runner=lambda *a, **k: "")


def test_social_publish_requires_live_readiness_proof(monkeypatch):
    monkeypatch.setenv("ARGUS_CONTENT_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("ARGUS_SOCIAL_PUBLISH_COMMAND", "echo posted")

    with pytest.raises(RuntimeError, match="live readiness proof missing"):
        handlers.run("social_publish", {"draft_id": "d1"}, runner=lambda *a, **k: "posted")

    proof = {
        "approval_proof": "owner approved content-approval:pr:2:publish:1783099493.667819",
        "durable_media": "asset stored in content/d1/image.png",
        "cta_route": "cta link resolves",
        "dm_activation": "dm automation active",
        "metricool_target": "Metricool brand and network selected",
        "connector_auth": "publisher connector auth checked",
    }
    ref = handlers.run("social_publish", {"draft_id": "d1", "live_readiness": proof},
                       runner=lambda *a, **k: "posted")

    assert ref == "posted"


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


def test_sync_pr_compares_current_default_branch_before_updating():
    runner = _fake_runner({
        ("gh", "repo", "view", "o/r"): json.dumps({
            "defaultBranchRef": {"name": "main"},
        }),
        ("gh", "pr", "view", "73"): json.dumps({
            "baseRefName": "main",
            "headRefName": "feature",
            "state": "OPEN",
        }),
        ("gh", "api", "repos/o/r/compare/main...feature"): json.dumps({
            "ahead_by": 2,
            "behind_by": 3,
            "status": "diverged",
        }),
    })

    ref = handlers.run("sync_pr", {"number": 73, "repo": "o/r"}, runner=runner)

    report = json.loads(ref)
    assert report == {
        "ahead": 2,
        "base": "main",
        "behind": 3,
        "head": "feature",
        "status": "diverged",
        "updated": True,
    }
    assert runner.calls[-1] == ["gh", "pr", "update-branch", "73", "-R", "o/r"]


def test_sync_pr_reports_current_branch_without_pushing():
    runner = _fake_runner({
        ("gh", "repo", "view", "o/r"): json.dumps({
            "defaultBranchRef": {"name": "main"},
        }),
        ("gh", "pr", "view", "73"): json.dumps({
            "baseRefName": "main",
            "headRefName": "feature",
            "state": "OPEN",
        }),
        ("gh", "api", "repos/o/r/compare/main...feature"): json.dumps({
            "ahead_by": 1,
            "behind_by": 0,
            "status": "ahead",
        }),
    })

    report = json.loads(handlers.run(
        "sync_pr", {"number": 73, "repo": "o/r"}, runner=runner))

    assert report["behind"] == 0
    assert report["updated"] is False
    assert not any(call[:3] == ["gh", "pr", "update-branch"] for call in runner.calls)


def test_sync_pr_rejects_pr_not_based_on_current_default_branch():
    runner = _fake_runner({
        ("gh", "repo", "view", "o/r"): json.dumps({
            "defaultBranchRef": {"name": "main"},
        }),
        ("gh", "pr", "view", "73"): json.dumps({
            "baseRefName": "staging",
            "headRefName": "feature",
            "state": "OPEN",
        }),
    })

    with pytest.raises(RuntimeError, match="current default branch main"):
        handlers.run("sync_pr", {"number": 73, "repo": "o/r"}, runner=runner)

    assert not any(call[:2] == ["gh", "api"] for call in runner.calls)


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
              "email_reply", "email_archive", "content_queue", "social_publish",
              "support_reply"):
        assert executor.risk_for(t) == "personal_outward", f"Expected personal_outward for {t}"


def test_risk_for_irreversible_types():
    from argus.v2.actions import executor
    for t in ("merge_pr", "deploy", "sync_pr"):
        assert executor.risk_for(t) == "irreversible_outward", f"Expected irreversible for {t}"


def test_risk_for_unknown_type_fails_safe():
    from argus.v2.actions import executor
    # Unknown types default to irreversible_outward (fail safe).
    assert executor.risk_for("rm") == "irreversible_outward"
    assert executor.risk_for("unknown_op") == "irreversible_outward"
