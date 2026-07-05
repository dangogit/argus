import os

from argus.v2.support.apps_script import EmailSummary
from argus.v2.support import cycle, state


def _support_cfg(tmp_path):
    from argus.v2.config import loader
    os.environ.setdefault("SUPPORT_KEY", "k")
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - { type: support_apps_script, name: luma-mail, team: luma, "
        "secret_ref: '${env:SUPPORT_KEY}', config: { url: 'https://support.test', "
        "notify_destination: 'cli:local' } }\n"
        "teams:\n"
        "  - name: luma\n"
        "    roles: [ { name: support, kind: worker, prompt: p } ]\n"
        "    pipeline: { stages: [support] }\n",
        encoding="utf-8",
    )
    return loader.load(cfg_path)


def _support_cfg_with_dev_pipeline(tmp_path):
    """A support team that also has a build pipeline (developer/qa/senior), so
    an escalate-dispatched investigation has a real stage-0 role to enqueue
    into (mirrors how a real product team is configured)."""
    from argus.v2.config import loader
    os.environ.setdefault("SUPPORT_KEY", "k")
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "  sources:\n"
        "    - { type: support_apps_script, name: luma-mail, team: luma, "
        "secret_ref: '${env:SUPPORT_KEY}', config: { url: 'https://support.test', "
        "notify_destination: 'cli:local', escalate_dispatch: true } }\n"
        "teams:\n"
        "  - name: luma\n"
        "    roles: [ { name: support, kind: worker, prompt: p },\n"
        "             { name: developer, kind: builder, prompt: p },\n"
        "             { name: qa, kind: judge, prompt: p },\n"
        "             { name: senior, kind: judge, prompt: p } ]\n"
        "    pipeline: { stages: [developer, qa, senior] }\n",
        encoding="utf-8",
    )
    return loader.load(cfg_path)


class ExplodingTransport:
    def archive(self, _thread_id):
        raise AssertionError("archive should not run")

    def mark_read(self, _thread_id):
        raise AssertionError("mark_read should not run")

    def read(self, _thread_id):
        raise AssertionError("read should not run")


class FakeTransport:
    def __init__(self, thread="From: user\nHow do I export?"):
        self.thread = thread
        self.replies = []
        self.reads = []
        self.thread_reads = []
        self.archived = []

    def archive(self, thread_id):
        self.archived.append(thread_id)

    def mark_read(self, thread_id):
        self.reads.append(thread_id)

    def read(self, thread_id):
        self.thread_reads.append(thread_id)
        return self.thread

    def reply(self, thread_id, body):
        self.replies.append((thread_id, body))


def test_classify_escalates_refunds():
    email = EmailSummary("T1", "u@example.com", "Refund", "please refund me")

    assert cycle.classify_email(email, own_domain="luma.ai") == "escalate"


def test_classify_sends_vendor_privacy_notice_to_llm_triage():
    email = EmailSummary(
        "T-policy",
        "Anthropic Team <notice@email.anthropic.com>",
        "We're updating our Privacy Policy",
        "We're writing to inform you about updates to our Privacy Policy.",
    )

    assert cycle.classify_email(
        email,
        automated_domains=["email.anthropic.com"],
    ) == "support"


def test_handle_email_does_not_archive_vendor_privacy_notice_before_read(monkeypatch, conn):
    team = type("Team", (), {"name": "luma", "project": None})()
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {"automated_domains": ["email.anthropic.com"]}
    email = EmailSummary(
        "T-policy",
        "Anthropic Team <notice@email.anthropic.com>",
        "We're updating our Privacy Policy",
        "updates to our Privacy Policy",
    )
    transport = FakeTransport()
    monkeypatch.setattr(
        cycle, "draft_decision",
        lambda *a, **k: cycle.DraftDecision(reply="",
                                            category="non_support",
                                            reason="vendor notice"),
    )

    result = cycle._handle_email(None, None, team, source, scfg, transport, email,
                                 cycle.SupportResult())

    assert result.archived == 1
    assert transport.thread_reads == ["T-policy"]
    assert transport.archived == ["T-policy"]
    assert transport.reads == ["T-policy"]


def test_vendor_notice_is_read_then_llm_classified_non_support(tmp_path, monkeypatch, conn):
    cfg = _support_cfg(tmp_path)
    team = cfg.team("luma")
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {
        "notify_destination": "cli:local",
        "automated_domains": ["email.anthropic.com"],
    }
    email = EmailSummary(
        "T-policy-llm",
        "Anthropic Team <notice@email.anthropic.com>",
        "We're updating our Privacy Policy",
        "updates to our Privacy Policy",
    )
    transport = FakeTransport(
        "From: Anthropic Team <notice@email.anthropic.com>\n"
        "We're writing to inform you about updates to our Privacy Policy."
    )
    captured = {}

    def fake_run_agent(_engine, prompt):
        captured["prompt"] = prompt
        return type("Out", (), {
            "text": '{"category":"non_support","reply":"","should_escalate":false,'
                    '"reason":"vendor policy notice","risk":"low","confidence":0.92}'
        })()

    monkeypatch.setattr(cycle, "run_agent", fake_run_agent)

    result = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                 cycle.SupportResult())
    conn.commit()

    assert result.archived == 1
    assert transport.thread_reads == ["T-policy-llm"]
    assert transport.archived == ["T-policy-llm"]
    assert "First classify the email" in captured["prompt"]
    assert "Privacy Policy" in captured["prompt"]
    assert state.latest_action("luma", "T-policy-llm") == "archived"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE type='notify'")
        assert cur.fetchone()[0] == 0


def test_handle_email_skips_already_escalated(monkeypatch):
    team = type("Team", (), {"name": "luma", "project": None})()
    source = type("Source", (), {"type": "support_apps_script"})()
    email = EmailSummary("T1", "u@example.com", "Help", "stuck")
    monkeypatch.setattr(cycle.state, "latest_action", lambda _project, _thread: "escalated")

    result = cycle._handle_email(None, None, team, source, {}, ExplodingTransport(), email, cycle.SupportResult())

    assert result.skipped == 1


def test_handle_email_skips_rejected_guidance(monkeypatch):
    team = type("Team", (), {"name": "luma", "project": None})()
    source = type("Source", (), {"type": "support_apps_script"})()
    email = EmailSummary("T1", "u@example.com", "Help", "stuck")
    monkeypatch.setattr(
        cycle.state, "latest_action",
        lambda _project, _thread: "guidance_rejected")

    result = cycle._handle_email(None, None, team, source, {}, ExplodingTransport(),
                                 email, cycle.SupportResult())

    assert result.skipped == 1


def test_run_project_skips_transport_failure(tmp_path, monkeypatch, capsys):
    cfg = _support_cfg(tmp_path)

    class FailingTransport:
        def __init__(self, **_kwargs):
            pass

        def list_unread(self, _limit):
            raise cycle.AppsScriptTransportError("apps-script list failed: HTTP 404")

    monkeypatch.setattr(cycle, "AppsScriptTransport", FailingTransport)

    result = cycle.run_project(None, cfg, "luma")

    assert result == cycle.SupportResult(skipped=10)
    assert "support: luma transport unavailable" in capsys.readouterr().err


def test_parse_and_plain_text_reply():
    parsed = cycle._parse_json('```json\n{"reply":"<p>Hi<br>There</p>","should_escalate":false}\n```')

    assert parsed["reply"] == "<p>Hi<br>There</p>"
    assert cycle._reply_to_text(parsed["reply"]) == "Hi\nThere"


def test_support_uncertain_reply_requests_guidance(tmp_path, monkeypatch, conn, cfg):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    team = type("Team", (), {"name": "luma", "project": None})()
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {"notify_destination": "cli:local"}
    email = EmailSummary("T1", "u@example.com", "Question", "help")
    transport = FakeTransport()
    monkeypatch.setattr(cycle, "draft_decision", lambda *a, **k: cycle.DraftDecision(
        reply="Try export.", needs_guidance=True, guidance_question="Can we export CSV?"))

    result = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                 cycle.SupportResult())
    conn.commit()

    assert result.escalated == 1
    assert state.latest_action("luma", "T1") == "guidance_requested"
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify'")
        text = cur.fetchone()[0]
        cur.execute("SELECT context_type, context_ref, payload->>'subject' "
                    "FROM conversation_contexts WHERE team_id='luma'")
        context = cur.fetchone()
    assert "Support guidance needed (luma)" in text
    assert "Can we export CSV?" in text
    assert "send:" in text
    assert context is not None
    assert context[0] == "support_case"
    assert context[1]
    assert context[2] == "Question"


def test_support_escalation_registers_guidance_context(tmp_path, monkeypatch, conn, cfg):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    team = type("Team", (), {"name": "luma", "project": None})()
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {"notify_destination": "cli:local"}
    email = EmailSummary("T-refund", "u@example.com", "Refund request",
                         "please refund unused subscription")
    transport = FakeTransport("From: u@example.com\nPlease refund my unused subscription.")

    result = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                 cycle.SupportResult())
    conn.commit()

    assert result.escalated == 1
    assert transport.reads == ["T-refund"]
    assert state.latest_action("luma", "T-refund") == "guidance_requested"
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify'")
        text = cur.fetchone()[0]
        cur.execute("SELECT status, payload->>'thread' FROM conversation_contexts "
                    "WHERE team_id='luma' AND channel_ref='cli:local'")
        context = cur.fetchone()
    assert "Support guidance needed (luma)" in text
    assert "Please refund my unused subscription" in text
    assert "send:" in text
    assert context == ("active", "From: u@example.com\nPlease refund my unused subscription.")


def test_support_auto_sends_low_risk_after_guidance(tmp_path, monkeypatch, conn, cfg):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    for idx in range(2):
        g = state.register_guidance_request("luma", f"OLD{idx}", "u@example.com",
                                            "Export", "q", "reply", "thread")
        state.resolve_guidance("luma", g.id, "Use export button.", status="sent")
    team = type("Team", (), {"name": "luma", "project": None})()
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {
        "notify_destination": "cli:local",
        "auto_send_low_risk": True,
        "auto_send_min_guidance": 2,
        "auto_send_confidence": 0.85,
    }
    email = EmailSummary("T2", "u@example.com", "Export", "help")
    transport = FakeTransport()
    monkeypatch.setattr(cycle, "draft_decision", lambda *a, **k: cycle.DraftDecision(
        reply="Use the export button.", risk="low", confidence=0.95))

    result = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                 cycle.SupportResult())

    assert result.sent == 1
    assert transport.replies == [("T2", "Use the export button.")]
    assert state.latest_action("luma", "T2") == "auto_replied"


def test_guidance_reply_learns_without_sending_by_default(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    guidance = state.register_guidance_request(
        "luma", "T3", "u@example.com", "Export", "q",
        "Use export button.", "thread")
    sent = []

    class Transport(FakeTransport):
        def __init__(self, **kwargs):
            super().__init__()

        def reply(self, thread_id, body):
            sent.append((thread_id, body))

    monkeypatch.setattr(cycle, "AppsScriptTransport", Transport)

    handled = cycle.handle_guidance_reply(
        conn, cfg, "luma", f"support {guidance.id} Use export button.")
    conn.commit()

    assert handled is True
    assert sent == []
    assert state.latest_action("luma", "T3") == "guidance_answered"
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify'")
        text = cur.fetchone()[0]
    assert "No customer reply sent" in text


def test_guidance_reply_learns_and_sends_with_explicit_send(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    guidance = state.register_guidance_request(
        "luma", "T3", "u@example.com", "Export", "q",
        "Use export button.", "thread")
    sent = []

    class Transport(FakeTransport):
        def __init__(self, **kwargs):
            super().__init__()

        def reply(self, thread_id, body):
            sent.append((thread_id, body))

    monkeypatch.setattr(cycle, "AppsScriptTransport", Transport)

    handled = cycle.handle_guidance_reply(
        conn, cfg, "luma", f"support {guidance.id} send: Use export button.")
    conn.commit()

    assert handled is True
    assert sent == [("T3", "Use export button.")]
    assert state.latest_action("luma", "T3") == "replied"


def test_context_bare_approval_sends_proposed_reply(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    guidance = state.register_guidance_request(
        "luma", "T7", "u@example.com", "Export", "Can we answer this?",
        "Click Settings then Export.", "From: u@example.com\nHow do I export?")
    req = state.guidance_request("luma", guidance.id)
    sent = []

    class Transport(FakeTransport):
        def __init__(self, **kwargs):
            super().__init__()

        def reply(self, thread_id, body):
            sent.append((thread_id, body))

    monkeypatch.setattr(cycle, "AppsScriptTransport", Transport)

    result = cycle.handle_context_message(
        conn, cfg, team_id="luma",
        context={"context_ref": guidance.id, "payload": req},
        text="send",
    )
    conn.commit()

    assert result.handled is True
    assert sent == [("T7", "Click Settings then Export.")]
    assert state.latest_action("luma", "T7") == "replied"


def test_context_retry_request_shows_email_without_learning(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    guidance = state.register_guidance_request(
        "luma", "T4", "u@example.com", "Export", "Can we answer this?",
        "", "From: u@example.com\nHow do I export my data?")
    req = state.guidance_request("luma", guidance.id)

    result = cycle.handle_context_message(
        conn,
        cfg,
        team_id="luma",
        context={"context_ref": guidance.id, "payload": req},
        text="try again",
    )

    assert result.handled is True
    assert result.context_status is None
    assert "Customer request:" in result.reply
    assert "How do I export my data?" in result.reply
    assert "Learned" not in result.reply
    assert state.latest_action("luma", "T4") == "guidance_requested"


def test_context_email_lookup_request_falls_through_without_learning(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    guidance = state.register_guidance_request(
        "luma", "T5", "u@example.com", "Export", "Can we answer this?",
        "", "From: u@example.com\nHow do I export my data?")
    req = state.guidance_request("luma", guidance.id)

    result = cycle.handle_context_message(
        conn,
        cfg,
        team_id="luma",
        context={"context_ref": guidance.id, "payload": req},
        text="find the email from Eesha Patel",
    )

    assert result.handled is False
    assert state.latest_action("luma", "T5") == "guidance_requested"
    assert state.guidance_request("luma", guidance.id)["status"] == "pending"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM knowledge WHERE team_id='luma' AND content ILIKE '%Eesha%'")
        assert cur.fetchone()[0] == 0


def test_context_ignore_instruction_becomes_support_rule(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    guidance = state.register_guidance_request(
        "luma", "T6", "Anthropic Team <notice@email.anthropic.com>",
        "We're updating our Privacy Policy", "What should we do?",
        "", "Privacy Policy update")
    req = state.guidance_request("luma", guidance.id)

    result = cycle.handle_context_message(
        conn,
        cfg,
        team_id="luma",
        context={"context_ref": guidance.id, "payload": req},
        text="this is not a support email, ignore it",
    )
    conn.commit()

    assert result.handled is True
    assert result.reply == "Learned. No customer reply sent."
    assert state.latest_action("luma", "T6") == "guidance_rejected"
    with conn.cursor() as cur:
        cur.execute("SELECT source, content FROM knowledge WHERE team_id='luma'")
        source, content = cur.fetchone()
    assert source == "support-rule"
    assert "ignore similar non-support vendor notices" in content


def test_support_prompt_includes_rules_skills_and_guidance(tmp_path, monkeypatch, conn):
    from argus.v2.knowledge import store

    cfg = _support_cfg(tmp_path)
    store.add(conn, cfg, scope="company", team_id=None, title="company rule",
              content="Owner rule: never send billing email automatically",
              source="owner-rule")
    store.add(conn, cfg, scope="team", team_id="luma", title="support lesson",
              content="For policy notices, ignore and archive.",
              source="support-guidance")
    conn.commit()
    captured = {}

    def fake_run_agent(_engine, prompt):
        captured["prompt"] = prompt
        return type("Out", (), {
            "text": '{"reply":"Use export.","should_escalate":false,'
                    '"risk":"low","confidence":0.9}'
        })()

    monkeypatch.setattr(cycle, "run_agent", fake_run_agent)

    decision = cycle.draft_decision(
        conn,
        cfg,
        "luma",
        thread="From: user\nprivacy policy question",
        sender="user@example.com",
        subject="privacy policy question",
        tone="friendly",
        repo=None,
    )

    assert decision is not None
    assert "OWNER RULES" in captured["prompt"]
    assert "never send billing email automatically" in captured["prompt"]
    assert "support email triage" in captured["prompt"].lower()
    assert "For policy notices, ignore and archive." in captured["prompt"]


def test_invalid_support_json_records_failure_reason(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)
    team = type("Team", (), {"name": "luma", "project": None})()
    source = type("Source", (), {"type": "support_apps_script"})()
    email = EmailSummary("T-json", "u@example.com", "Help", "help")
    transport = FakeTransport("From: u@example.com\nHelp me export.")
    monkeypatch.setattr(cycle, "run_agent",
                        lambda *_a, **_k: type("Out", (), {"text": "not json"})())

    result = cycle._handle_email(conn, cfg, team, source, {"notify_destination": "cli:local"},
                                 transport, email, cycle.SupportResult())
    conn.commit()

    assert result.escalated == 1
    with conn.cursor() as cur:
        cur.execute("SELECT reason FROM support_thread_events "
                    "WHERE project='luma' AND thread_id='T-json' AND action='failed'")
        assert cur.fetchone()[0] == "invalid_json"
        cur.execute("SELECT question FROM support_guidance WHERE project='luma' "
                    "AND thread_id='T-json'")
        assert "Draft failed (invalid_json)" in cur.fetchone()[0]


def test_escalate_dispatch_off_does_not_enqueue_investigation(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg(tmp_path)  # escalate_dispatch not set -> default False
    team = cfg.team("luma")
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {"notify_destination": "cli:local"}
    email = EmailSummary("T-esc-off", "u@example.com", "Refund",
                         "please refund me, this is broken")
    transport = FakeTransport("From: u@example.com\nPlease refund me, this is broken.")

    result = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                 cycle.SupportResult())
    conn.commit()

    assert result.escalated == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE team_id='luma'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM events WHERE source='support-escalate'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify'")
        text = cur.fetchone()[0]
    assert "auto-dispatched" not in text


def test_escalate_dispatch_on_enqueues_investigation_once(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg_with_dev_pipeline(tmp_path)
    team = cfg.team("luma")
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {
        "notify_destination": "cli:local",
        "escalate_dispatch": True,
    }
    email = EmailSummary("T-esc-on", "u@example.com", "My instance setup is broken",
                         "please refund me, my instance setup is broken")
    transport = FakeTransport(
        "From: u@example.com\nMy instance setup is broken, please refund me."
    )

    result = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                 cycle.SupportResult())
    conn.commit()

    assert result.escalated == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE team_id='luma'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM events WHERE source='support-escalate'")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT payload->>'text' FROM events WHERE source='support-escalate'")
        task_text = cur.fetchone()[0]
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='notify'")
        notify_text = cur.fetchone()[0]

    assert "Investigate this customer complaint" in task_text
    assert "My instance setup is broken" in task_text
    assert "u@example.com" in task_text
    assert "auto-dispatched" in notify_text
    assert "team pipeline" in notify_text


def test_escalate_dispatch_rerun_same_thread_does_not_reenqueue(tmp_path, monkeypatch, conn):
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg_with_dev_pipeline(tmp_path)
    team = cfg.team("luma")
    source = type("Source", (), {"type": "support_apps_script"})()
    scfg = {"notify_destination": "cli:local", "escalate_dispatch": True}
    email = EmailSummary("T-esc-rerun", "u@example.com", "Broken setup",
                         "please refund me, broken setup")
    transport = FakeTransport("From: u@example.com\nBroken setup, please refund me.")

    first = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                cycle.SupportResult())
    conn.commit()
    assert first.escalated == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE team_id='luma'")
        assert cur.fetchone()[0] == 1

    # Later cycle sees the same thread again (e.g. rerun / redelivery). The
    # existing skip-on-terminal-action check in _handle_email keeps this from
    # re-entering the escalate branch at all.
    second = cycle._handle_email(conn, cfg, team, source, scfg, transport, email,
                                 cycle.SupportResult())
    conn.commit()

    assert second.skipped == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE team_id='luma'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM events WHERE source='support-escalate'")
        assert cur.fetchone()[0] == 1


def test_escalate_dispatch_direct_call_is_idempotent_per_thread(tmp_path, monkeypatch, conn):
    """Belt-and-suspenders: even bypassing the _handle_email skip guard (e.g. a
    future caller or a race), _dispatch_investigation itself must not open a
    second pipeline request for the same thread_id."""
    monkeypatch.setenv("ARGUS_SUPPORT_DIR", str(tmp_path / "support"))
    cfg = _support_cfg_with_dev_pipeline(tmp_path)
    scfg = {"escalate_dispatch": True}
    email = EmailSummary("T-esc-direct", "u@example.com", "Broken",
                         "please help, broken")
    thread = "From: u@example.com\nSomething is broken, please help."

    first = cycle._dispatch_investigation(conn, cfg, "luma", scfg, email, thread)
    conn.commit()
    second = cycle._dispatch_investigation(conn, cfg, "luma", scfg, email, thread)
    conn.commit()

    assert first is True
    assert second is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests WHERE team_id='luma'")
        assert cur.fetchone()[0] == 1


def test_customer_message_strips_quotes_and_signature():
    thread = (
        "Hi, I was charged twice this month.\n"
        "Please check my account.\n"
        "--\n"
        "Dana Cohen\n"
        "Sent from my iPhone\n"
        "On Tue, Jul 1, 2026 at 9:00 AM Support <s@x.com> wrote:\n"
        "> Thanks for reaching out\n"
        "> We will look into it\n"
    )
    msg = cycle._customer_message(thread)
    assert "charged twice" in msg
    assert "wrote:" not in msg
    assert ">" not in msg
    assert "Dana Cohen" not in msg


def test_customer_message_truncates_and_falls_back():
    long = "word " * 300
    msg = cycle._customer_message(long, limit=100)
    assert len(msg) <= 104 and msg.endswith("...")
    # All-quoted thread falls back to the generic excerpt instead of empty.
    quoted = "> only quoted content\n> nothing else\n"
    assert cycle._customer_message(quoted) != ""
