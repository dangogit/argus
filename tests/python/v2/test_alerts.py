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
