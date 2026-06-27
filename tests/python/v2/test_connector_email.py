from argus.v2.connectors.email_imap import EmailConnector

# Each "raw" item is (uid, headers dict, body str) -- what fetch() yields.
RAW = [
    (101, {"Message-ID": "<a@x>", "From": "user@co.com", "Subject": "Login broken"}, "cant log in"),
    (102, {"Message-ID": "<b@x>", "From": "vip@co.com", "Subject": "Refund please"}, "where is my refund"),
]


def test_parse_maps_emails_to_signals_and_advances_uid():
    signals, cursor = EmailConnector.parse(RAW, {})
    assert [s.fingerprint for s in signals] == ["<a@x>", "<b@x>"]
    assert signals[0].payload["subject"] == "Login broken"
    assert signals[0].payload["from"] == "user@co.com"
    assert cursor["last_uid"] == 102


def test_parse_skips_seen_uids():
    signals, _ = EmailConnector.parse(RAW, {"last_uid": 101})
    assert [s.fingerprint for s in signals] == ["<b@x>"]
