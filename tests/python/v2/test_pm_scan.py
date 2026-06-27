from argus.v2.pm import scan


def test_scan_diff_flags_added_secret_only():
    findings = scan.scan_diff(
        "diff --git a/x b/x\n"
        "-token = 'sk-oldsecretoldsecretoldsecret'\n"
        "+token = 'sk-newsecretnewsecretnewsecret'\n"
    )

    assert scan.has_critical(findings)
    assert [f.rule for f in findings] == ["secret-token", "secret-literal"]


def test_scan_diff_ignores_removed_secret():
    findings = scan.scan_diff(
        "diff --git a/x b/x\n"
        "-token = 'sk-oldsecretoldsecretoldsecret'\n"
        "+token = os.environ['TOKEN']\n"
    )

    assert findings == []
