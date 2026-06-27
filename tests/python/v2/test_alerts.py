from argus.v2 import alerts, cli


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
