from argus.v2.actions import executor
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
                  '"body":"Body","summary_short":"Fixed x","checks":"QA: pass",'
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
    assert "PR ready: Fix x" in text
    assert "https://github.test/pull/9" in text


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


def test_low_disk_whatsapp_notify_suppresses_recent_same_fingerprint(conn, cfg, monkeypatch):
    from argus.v2.channels import send as _send

    def fail_deliver(_cfg, _dest, _text):
        raise AssertionError("duplicate low-disk notification should not send")

    monkeypatch.setattr(_send, "deliver", fail_deliver)
    payload = (
        '{"text":"Argus health: system issue detected",'
        '"findings":[{"severity":"warn","fingerprint":"disk:low:home",'
        '"message":"low disk space","payload":{"updated_at":"2026-07-03T08:30:00Z"}}]}'
    )
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO actions
               (team_id, type, risk, destination_ref, idempotency_key,
                status, provider_ref, payload)
               VALUES ('dev','notify','reversible_internal','whatsapp:owner',
                       'disk-first','done','wa:first',%s::jsonb)""",
            (payload,),
        )
        cur.execute(
            """INSERT INTO actions
               (team_id, type, risk, destination_ref, idempotency_key, payload)
               VALUES ('dev','notify','reversible_internal','whatsapp:owner',
                       'disk-dup',%s::jsonb)""",
            (payload,),
        )
    conn.commit()

    executor.process_proposed(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, provider_ref, payload->>'suppression_reason' "
            "FROM actions WHERE idempotency_key='disk-dup'",
        )
        action_row = cur.fetchone()
        cur.execute(
            "SELECT channel, message FROM alerts "
            "WHERE fingerprint='notify-suppressed:disk:low:home'",
        )
        alert_row = cur.fetchone()
    assert action_row == (
        "done",
        "suppressed:disk:low:home",
        "duplicate_low_disk_notification",
    )
    assert alert_row == (
        "log",
        "suppressed duplicate low-disk notification: disk:low:home",
    )


def test_low_disk_whatsapp_notify_sends_new_updated_at_evidence(conn, cfg, monkeypatch):
    from argus.v2.channels import send as _send

    sent = []

    def deliver(_cfg, dest, text):
        sent.append((dest, text))
        return "wa:newer"

    monkeypatch.setattr(_send, "deliver", deliver)
    old_payload = (
        '{"text":"Argus health: system issue detected",'
        '"findings":[{"severity":"warn","fingerprint":"disk:low:home",'
        '"message":"low disk space","payload":{"updated_at":"2026-07-03T08:30:00Z"}}]}'
    )
    newer_payload = (
        '{"text":"Argus health: system issue detected",'
        '"findings":[{"severity":"warn","fingerprint":"disk:low:home",'
        '"message":"low disk space","payload":{"updated_at":"2026-07-03T08:35:00Z"}}]}'
    )
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO actions
               (team_id, type, risk, destination_ref, idempotency_key,
                status, provider_ref, payload)
               VALUES ('dev','notify','reversible_internal','whatsapp:owner',
                       'disk-first','done','wa:first',%s::jsonb)""",
            (old_payload,),
        )
        cur.execute(
            """INSERT INTO actions
               (team_id, type, risk, destination_ref, idempotency_key, payload)
               VALUES ('dev','notify','reversible_internal','whatsapp:owner',
                       'disk-newer',%s::jsonb)""",
            (newer_payload,),
        )
    conn.commit()

    executor.process_proposed(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, provider_ref, payload->>'suppression_reason' "
            "FROM actions WHERE idempotency_key='disk-newer'",
        )
        action_row = cur.fetchone()
    assert sent == [("whatsapp:owner", "Argus health: system issue detected")]
    assert action_row == ("done", "wa:newer", None)


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
