from argus.v2 import alerts, cli


LOW_DISK_ESCALATION_EVIDENCE = [
    "3cadf2de00d9a29b8815ea40",
    "7072881ea381099c813c2fbf",
    "converse:3b272dbf-93c6-4848-9a3a-4ef75f24054b",
    "4f04c60dc16c4f490917d55c",
    "retro-change:3cadf2de00d9a29b8815ea40",
]


def test_alert_record_routes_error_to_whatsapp(conn):
    alert_id = alerts.record(
        conn,
        severity="error",
        project="context",
        fingerprint="context-down",
        message="source unavailable",
    )
    conn.commit()

    rows = alerts.list_alerts(conn)

    assert rows[0].id == alert_id
    assert rows[0].severity == "error"
    assert rows[0].channel == "whatsapp"
    assert rows[0].project == "context"


def test_alert_record_escalates_same_day_low_disk_after_third_occurrence(conn):
    fingerprint = "disk:low:3cadf2de00d9a29b8815ea40"

    for evidence in LOW_DISK_ESCALATION_EVIDENCE[:3]:
        alerts.record(
            conn,
            severity="warn",
            project="general",
            fingerprint=fingerprint,
            message="low disk space",
            payload={"evidence_fingerprint": evidence},
        )
        conn.commit()

    alerts.record(
        conn,
        severity="warn",
        project="general",
        fingerprint=fingerprint,
        message="low disk space",
        payload={"evidence_fingerprint": LOW_DISK_ESCALATION_EVIDENCE[3]},
    )
    conn.commit()

    alerts.record(
        conn,
        severity="warn",
        project="other",
        fingerprint=fingerprint,
        message="low disk space",
        payload={"evidence_fingerprint": LOW_DISK_ESCALATION_EVIDENCE[4]},
    )
    conn.commit()

    rows = [
        row for row in alerts.list_alerts(conn, project="general")
        if row.fingerprint == fingerprint
    ]
    other = [
        row for row in alerts.list_alerts(conn, project="other")
        if row.fingerprint == fingerprint
    ]

    assert [row.severity for row in reversed(rows)] == [
        "warn",
        "warn",
        "warn",
        "error",
    ]
    assert [row.payload["evidence_fingerprint"] for row in reversed(rows)] == (
        LOW_DISK_ESCALATION_EVIDENCE[:4]
    )
    assert other[0].severity == "warn"


def test_alert_cli_add_and_list(conn, pg_dsn, monkeypatch, capsys):
    monkeypatch.setenv("ARGUS_DB_DSN", pg_dsn)

    rc = cli.main([
        "alert", "add",
        "--severity", "warn",
        "--project", "content",
        "--fingerprint", "content-1",
        "--message", "draft failed",
    ])
    assert rc == 0
    assert "alert " in capsys.readouterr().out

    rc = cli.main(["alert", "list", "--project", "content"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "\twarn\tlog\tcontent\tcontent-1\tdraft failed" in out


def test_alert_record_collapses_unchanged_lineage_state(conn):
    payload = {
        "collapse_lineage": True,
        "original_request_lineage": "converse:cb8313d2-be1f-4fd4-9098-805913ebd9f2",
        "readiness_signal": "missing-approval",
        "blocker_key": "metricool-target",
        "escalation_level": "watch",
    }

    first = alerts.record(
        conn,
        severity="warn",
        project="general",
        fingerprint="7072881ea381099c813c2fbf",
        message="content watchdog blocked",
        payload=payload,
    )
    conn.commit()
    repeated = alerts.record(
        conn,
        severity="warn",
        project="general",
        fingerprint="4f04c60dc16c4f490917d55c",
        message="same blocked content watchdog",
        payload=payload,
    )
    conn.commit()

    assert first is not None
    assert repeated is None
    rows = alerts.list_alerts(conn, project="general")
    assert [row.fingerprint for row in rows] == ["7072881ea381099c813c2fbf"]


def test_alert_record_allows_lineage_state_changes(conn):
    base = {
        "collapse_lineage": True,
        "lineage": "retro-change:3cadf2de00d9a29b8815ea40",
        "readiness_signal": "approval-missing",
        "blocker": "connector-auth",
        "escalation": "watch",
    }
    alerts.record(
        conn,
        severity="warn",
        project="general",
        fingerprint="retro-change:9004fb4f30fac1dd8cbbc7b5",
        message="initial stuck alert",
        payload=base,
    )
    conn.commit()

    changed = [
        ("readiness-ok", "connector-auth", "watch"),
        ("readiness-ok", "durable-media", "watch"),
        ("readiness-ok", "durable-media", "escalate"),
    ]
    for readiness, blocker, escalation in changed:
        alert_id = alerts.record(
            conn,
            severity="warn",
            project="general",
            fingerprint=f"converse:3b272dbf:{readiness}:{blocker}:{escalation}",
            message="changed stuck alert",
            payload={
                **base,
                "readiness_signal": readiness,
                "blocker": blocker,
                "escalation": escalation,
            },
        )
        conn.commit()
        assert alert_id is not None

    rows = alerts.list_alerts(conn, project="general")
    assert len(rows) == 4
