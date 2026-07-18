import json

from argus.v2.actions import executor
from argus.v2.config.schema import Autonomy
from argus.v2.ingress import events
from argus.v2.orchestrator import pipeline


def _proposed(conn, risk, request_id, idem="a0", dest=None, payload="{}",
              status="proposed"):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO actions (request_id, team_id, type, risk, destination_ref,
                                    idempotency_key, payload, status)
               VALUES (%s,'dev','notify',%s,%s,%s,%s::jsonb,%s) RETURNING id""",
            (request_id, risk, dest, idem, payload, status))
        return str(cur.fetchone()[0])


def _request(conn, cfg):
    eid = events.ingest_message(conn, cfg, team="dev", source="cli",
                                dedup_key="m1", text="t")
    return pipeline.open_request(conn, cfg, event_id=eid, team_id="dev",
                                 conversation_id=None)


def test_action_override_is_narrower_than_risk_policy():
    autonomy = Autonomy(
        irreversible_outward="approval",
        actions={"merge_pr": "auto"},
    )

    assert executor._mode_for(
        autonomy, "merge_pr", "irreversible_outward") == "auto"
    assert executor._mode_for(
        autonomy, "deploy", "irreversible_outward") == "approval"


def test_ready_pr_is_reversible_internal():
    assert executor.risk_for("ready_pr") == "reversible_internal"


def test_ownership_mutations_are_absent_from_conversational_allowlists():
    forbidden = {"ready_pr", "merge_pr", "deploy"}
    allowlists = {
        name: values
        for name, values in vars(executor).items()
        if name.startswith("_CONVERSE") and name.endswith("ALLOWLIST")
    }

    assert allowlists
    for name, values in allowlists.items():
        assert forbidden.isdisjoint(values), name


def test_reversible_action_auto_executes(conn, cfg):
    rid = _request(conn, cfg)
    _proposed(conn, "reversible_internal", rid); conn.commit()
    executor.process_proposed(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key='a0'")
        assert cur.fetchone()[0] == "done"


def test_irreversible_action_pauses_for_approval(conn, cfg):
    rid = _request(conn, cfg)
    _proposed(conn, "irreversible_outward", rid); conn.commit()
    executor.process_proposed(conn, cfg); conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key='a0'")
        assert cur.fetchone()[0] == "awaiting_approval"
        cur.execute("SELECT count(*) FROM approvals")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT status FROM requests WHERE id=%s", (rid,))
        assert cur.fetchone()[0] == "awaiting_approval"


def test_executor_is_idempotent(conn, cfg):
    rid = _request(conn, cfg)
    _proposed(conn, "reversible_internal", rid); conn.commit()
    executor.process_proposed(conn, cfg); conn.commit()
    n = executor.process_proposed(conn, cfg); conn.commit()  # nothing left
    assert n == 0


def test_open_pr_enqueues_control_summary(conn, cfg):
    rid = _request(conn, cfg)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO actions (request_id, team_id, type, risk, destination_ref,
                                    idempotency_key, payload)
               VALUES (%s,'dev','open_pr','reversible_internal','fake:chat','open-pr',
                       %s)""",
            (rid, '{"branch":"b","base":"main","remote":"origin","title":"Fix x",'
                  '"body":"Body","request":"bug report 123: checkout broken",'
                  '"summary_short":"Fixed x","checks":"QA: pass",'
                  '"risk_summary":"low","cwd":"/tmp"}'))
    conn.commit()

    def runner(argv, cwd=None):
        return "https://github.test/pull/9\n" if argv[:3] == ["gh", "pr", "create"] else ""

    executor.process_proposed(conn, cfg, runner=runner)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT provider_ref FROM actions WHERE idempotency_key='open-pr'")
        assert cur.fetchone()[0] == "https://github.test/pull/9"
        cur.execute("SELECT destination_ref, payload->>'text' FROM actions WHERE type='notify'")
        dest, text = cur.fetchone()
    assert dest == "fake:chat"
    assert "Argus PR ready" in text
    assert 'Request: "bug report 123: checkout broken"' in text
    assert 'Fix: "Fixed x"' in text
    assert "Checks:\n- QA: pass" in text
    assert "Status: checked and verified" in text
    assert "Review notes:" not in text
    assert "PR ready:" not in text
    assert "Risk:" not in text
    assert "https://github.test/pull/9" in text


def test_open_pr_notice_formats_review_failure():
    text = executor._open_pr_notice_text({
        "request": "bug report 123: checkout broken",
        "summary_short": "Fixed x",
        "checks": "QA: pass; Browser: fail; Senior: no decision",
        "risk_summary": (
            "needs review: browser_verify failed; "
            "browser_verify did not pass after 1 rework attempt(s). "
            "Blocking issue: browser run error: Command "
            "['/Users/danielmini/.local/bin/hermes', '-z', 'long prompt'] "
            "timed out after 300 seconds"
        ),
    }, "https://github.test/pull/9")

    assert "Argus PR needs review" in text
    assert "Status: needs review, browser verification failed" in text
    assert "Checks:\n- QA: pass\n- Browser: fail\n- Senior: no decision" in text
    assert "Review notes:" in text
    assert "browser command timed out after 300 seconds" in text
    assert "Command [" not in text


def test_reply_send_timeout_is_retryable(conn, cfg, monkeypatch):
    """A reply whose deliver() times out must not crash process_proposed, and
    the action must stay retryable (proposed/approved) so the next tick redelivers
    it instead of silently dropping the reply."""
    import httpx
    from argus.v2.channels import send as _send

    def boom(_cfg, _dest, _text):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(_send, "deliver", boom)

    rid = _request(conn, cfg)
    _proposed(conn, "reversible_internal", rid, idem="timeout-reply", dest="fake:chat")
    conn.commit()

    # Must NOT raise httpx.ReadTimeout out of the drain loop.
    executor.process_proposed(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key='timeout-reply'")
        status = cur.fetchone()[0]
    assert status in ("proposed", "approved"), f"expected retryable, got {status!r}"


def test_outward_channel_reply_requires_approval(conn, tmp_path):
    from argus.v2.config import loader
    from argus.v2.channels import fake

    fake.SENT.clear()
    y = tmp_path / "c.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: outward, channel_id: customer } ]\n"
    )
    cfg_outward = loader.load(y)
    rid = _request(conn, cfg_outward)
    _proposed(conn, "reversible_internal", rid, idem="out-reply",
              dest="fake:customer")
    conn.commit()

    executor.process_proposed(conn, cfg_outward)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT risk, status FROM actions WHERE idempotency_key='out-reply'")
        assert cur.fetchone() == ("irreversible_outward", "awaiting_approval")
        cur.execute("SELECT count(*) FROM approvals")
        assert cur.fetchone()[0] == 1
    assert fake.SENT == []


def _quiet_cfg(tmp_path, quiet_hours="'00:00-23:59'"):
    from argus.v2.config import loader
    y = tmp_path / "quiet.yaml"
    y.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    notifications:\n"
        "      timezone: UTC\n"
        f"      quiet_hours: {quiet_hours}\n"
        "      quiet_hours_delivery: hold\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: chat } ]\n",
        encoding="utf-8",
    )
    return loader.load(y)


def test_non_urgent_notify_holds_during_quiet_hours(conn, tmp_path):
    cfg_quiet = _quiet_cfg(tmp_path)
    rid = _request(conn, cfg_quiet)
    _proposed(conn, "reversible_internal", rid, idem="quiet", dest="fake:chat",
              payload='{"text":"PM result: no code fix warranted"}')
    conn.commit()

    executor.process_proposed(conn, cfg_quiet)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status, payload ? 'quiet_hold_until' "
                    "FROM actions WHERE idempotency_key='quiet'")
        assert cur.fetchone() == ("held", True)


def test_held_notify_releases_after_quiet_until(conn, tmp_path):
    from argus.v2.channels import fake

    fake.SENT.clear()
    cfg_quiet = _quiet_cfg(tmp_path, "false")
    rid = _request(conn, cfg_quiet)
    _proposed(
        conn,
        "reversible_internal",
        rid,
        idem="held-old",
        dest="fake:chat",
        payload='{"text":"held message","quiet_hold_until":"2000-01-01T00:00:00+00:00"}',
        status="held",
    )
    conn.commit()

    executor.process_proposed(conn, cfg_quiet)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key='held-old'")
        assert cur.fetchone()[0] == "done"
    assert fake.SENT == [("chat", "held message")]


def test_urgent_notify_bypasses_quiet_hours(conn, tmp_path):
    from argus.v2.channels import fake

    fake.SENT.clear()
    cfg_quiet = _quiet_cfg(tmp_path)
    rid = _request(conn, cfg_quiet)
    _proposed(conn, "reversible_internal", rid, idem="urgent", dest="fake:chat",
              payload='{"text":"refund request cannot access account"}')
    conn.commit()

    executor.process_proposed(conn, cfg_quiet)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key='urgent'")
        assert cur.fetchone()[0] == "done"
    assert fake.SENT == [("chat", "refund request cannot access account")]


def test_generic_pm_error_notify_stays_held(conn, tmp_path):
    from argus.v2.channels import fake

    fake.SENT.clear()
    cfg_quiet = _quiet_cfg(tmp_path)
    rid = _request(conn, cfg_quiet)
    _proposed(conn, "reversible_internal", rid, idem="pm-error", dest="fake:chat",
              payload='{"text":"PM result: Error. No code fix warranted."}')
    conn.commit()

    executor.process_proposed(conn, cfg_quiet)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE idempotency_key='pm-error'")
        assert cur.fetchone()[0] == "held"
    assert fake.SENT == []


def test_duplicate_low_disk_whatsapp_notify_suppresses_second_send(
    conn, tmp_path, monkeypatch
):
    from argus.v2.channels import send as _send
    from argus.v2.config import loader

    y = tmp_path / "wa.yaml"
    y.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    autonomy:\n"
            "      reversible_internal: auto\n"
            "      irreversible_outward: approval\n"
            "    notifications:\n"
            "      quiet_hours: false\n"
            "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: whatsapp, role: control, channel_id: owner } ]\n",
        encoding="utf-8",
    )
    cfg_wa = loader.load(y)
    calls = []

    def deliver(_cfg, destination_ref, text):
        calls.append((destination_ref, text))
        return f"wa:{len(calls)}"

    monkeypatch.setattr(_send, "deliver", deliver)
    payload = json.dumps({
        "text": "low disk space under /Users/danielmini: 2.1 GB free",
        "system_health_fingerprints": ["disk:low:argus-run"],
        "evidence": [
            "retro-change:2851e16d8281fc8ab7c28e49",
            "converse:cb8313d2-be1f-4fd4-9098-805913ebd9f2",
        ],
    })
    with conn.cursor() as cur:
        for idem in ("low-disk-1", "low-disk-2"):
            cur.execute(
                """
                INSERT INTO actions (team_id, type, risk, destination_ref,
                                     idempotency_key, payload)
                VALUES ('dev','notify','reversible_internal','whatsapp:owner',
                        %s,%s::jsonb)
                """,
                (idem, payload),
            )
    conn.commit()

    executor.process_proposed(conn, cfg_wa)
    conn.commit()

    assert calls == [
        (
            "whatsapp:owner",
            "low disk space under /Users/danielmini: 2.1 GB free",
        )
    ]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, provider_ref, payload->>'suppressed_reason' "
            "FROM actions WHERE idempotency_key='low-disk-2'"
        )
        status, provider_ref, reason = cur.fetchone()
        cur.execute(
            "SELECT payload->>'suppressed_fingerprint' FROM alerts "
            "WHERE fingerprint LIKE 'disk:low:send-suppressed:%'"
        )
        suppressed_fingerprint = cur.fetchone()[0]
    assert status == "done"
    assert provider_ref.startswith("suppressed:duplicate_low_disk:")
    assert reason == "duplicate_low_disk_notify"
    assert suppressed_fingerprint == "disk:low:argus-run"
