import subprocess

from argus.v2 import alerts, system_health
from argus.v2.config import loader
from argus.v2.ingress import events
from argus.v2.orchestrator import reconcile


def _cfg_path(tmp_path, *, secret_ref=""):
    secret = f", secret_ref: \"{secret_ref}\"" if secret_ref else ""
    path = tmp_path / "argus.yaml"
    path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults:\n"
        "    engine: { engine: echo }\n"
        "    support: { token: \"${env:ARGUS_TEST_SUPPORT_TOKEN}\" }\n"
        "teams:\n"
        "  - name: general\n"
        "    roles: [ { name: manager, kind: front, prompt: p } ]\n"
        "    pipeline: { stages: [manager] }\n"
        f"    channels: [ {{ type: fake, role: control, channel_id: general{secret} }} ]\n",
        encoding="utf-8",
    )
    return path


def test_missing_env_refs_reports_only_names(tmp_path, monkeypatch):
    path = _cfg_path(tmp_path, secret_ref="${env:ARGUS_TEST_WA_KEY}")
    monkeypatch.setenv("ARGUS_TEST_WA_KEY", "actual-secret-value")
    monkeypatch.delenv("ARGUS_TEST_SUPPORT_TOKEN", raising=False)

    findings = system_health.missing_env_findings(path)

    assert len(findings) == 1
    assert findings[0].fingerprint == "missing-env:ARGUS_TEST_SUPPORT_TOKEN"
    assert "ARGUS_TEST_SUPPORT_TOKEN" in findings[0].message
    assert "actual-secret-value" not in findings[0].message


def test_launchd_findings_flag_down_service_not_periodic_stale_exit():
    """A keep-alive service with no PID is down -> flagged. A periodic job
    (watchdog) idle with a stale non-zero last exit is NOT a health signal and
    must not be flagged (that was the hourly false-positive spam)."""
    def runner(argv, **kwargs):
        assert argv == ["launchctl", "list"]
        return subprocess.CompletedProcess(
            argv, 0,
            "-\t1\tcom.argus.serve\n"      # keep-alive service, DOWN
            "-\t1\tcom.argus.watchdog\n"   # periodic job, idle + stale exit (ignore)
            "123\t0\tcom.argus.up\n", "")  # keep-alive service, running

    findings = system_health.launchd_findings(
        runner=runner, service_labels={"com.argus.serve", "com.argus.up"})

    assert [f.fingerprint for f in findings] == ["launchd:com.argus.serve:down"]
    assert "com.argus.serve" in findings[0].message
    assert "watchdog" not in " ".join(f.message for f in findings)


def test_launchd_findings_periodic_nonzero_exit_is_not_flagged():
    """The exact owner-reported case: poll exited 1 once, idle now. No alert."""
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "-\t1\tcom.argus.poll\n", "")
    # poll is not a keep-alive service -> empty service set -> nothing flagged.
    assert system_health.launchd_findings(runner=runner, service_labels=set()) == []


def test_memory_findings_flags_high_rss_argus_service_when_memory_low(monkeypatch):
    monkeypatch.setenv("ARGUS_HEALTH_MIN_MEMORY_FREE_PERCENT", "20")
    monkeypatch.setenv("ARGUS_HEALTH_MAX_SERVICE_RSS_MB", "512")

    def runner(argv, **kwargs):
        if argv == ["memory_pressure"]:
            return subprocess.CompletedProcess(
                argv, 0, "System-wide memory free percentage: 8%\n", "")
        if argv[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                argv, 0,
                "   111   -15  com.argus.up\n"
                "   222   -15  com.argus.work-pipeline\n"
                "     0     0  com.argus.poll\n", "")
        raise AssertionError(argv)

    rss = {111: 300 * 1024 * 1024, 222: 900 * 1024 * 1024}
    findings = system_health.memory_findings(
        runner=runner,
        rss_reader=lambda pid: rss.get(pid),
    )

    assert len(findings) == 1
    assert findings[0].fingerprint == "memory:com.argus.work-pipeline:rss-high"
    assert "cap 512 MB" in findings[0].message
    assert findings[0].payload["rss_mb"] == 900


def test_memory_findings_low_memory_without_argus_culprit_is_warn(monkeypatch):
    monkeypatch.setenv("ARGUS_HEALTH_MIN_MEMORY_FREE_PERCENT", "20")
    monkeypatch.setenv("ARGUS_HEALTH_MAX_SERVICE_RSS_MB", "512")

    def runner(argv, **kwargs):
        if argv == ["memory_pressure"]:
            return subprocess.CompletedProcess(
                argv, 0, "System-wide memory free percentage: 8%\n", "")
        if argv[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(argv, 0, "   111   -15  com.argus.up\n", "")
        raise AssertionError(argv)

    findings = system_health.memory_findings(
        runner=runner,
        rss_reader=lambda pid: 300 * 1024 * 1024,
    )

    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert findings[0].fingerprint == "memory:low:no-argus-culprit"
    assert "no com.argus.* service above 512 MB RSS" in findings[0].message


def test_remediate_findings_restarts_only_high_rss_argus_service(monkeypatch):
    monkeypatch.setattr(system_health.sys, "platform", "darwin")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    finding = system_health.Finding(
        severity="error",
        fingerprint="memory:com.argus.work-pipeline:rss-high",
        message="low host memory",
        payload={"label": "com.argus.work-pipeline"},
    )

    restarted = system_health.remediate_findings(
        [finding], enabled=True, runner=runner)

    assert restarted == ["com.argus.work-pipeline"]
    assert calls == [[
        "launchctl", "kickstart", "-k",
        f"gui/{system_health.os.getuid()}/com.argus.work-pipeline",
    ]]


def test_remediate_findings_ignores_non_argus_label(monkeypatch):
    monkeypatch.setattr(system_health.sys, "platform", "darwin")
    calls = []
    finding = system_health.Finding(
        severity="error",
        fingerprint="memory:com.apple.Finder:rss-high",
        message="low host memory",
        payload={"label": "com.apple.Finder"},
    )

    restarted = system_health.remediate_findings(
        [finding],
        enabled=True,
        runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    assert restarted == []
    assert calls == []


def test_remediate_findings_disabled_by_default(monkeypatch):
    monkeypatch.setattr(system_health.sys, "platform", "darwin")
    monkeypatch.delenv("ARGUS_HEALTH_AUTO_RESTART", raising=False)
    calls = []
    finding = system_health.Finding(
        severity="error",
        fingerprint="memory:com.argus.work-pipeline:rss-high",
        message="low host memory",
        payload={"label": "com.argus.work-pipeline"},
    )

    restarted = system_health.remediate_findings(
        [finding],
        runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )

    assert restarted == []
    assert calls == []


def test_remediate_findings_cooldown_throttles_restart_loop(conn, monkeypatch):
    monkeypatch.setattr(system_health.sys, "platform", "darwin")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    finding = system_health.Finding(
        severity="error",
        fingerprint="memory:com.argus.work-pipeline:rss-high",
        message="low host memory",
        payload={"label": "com.argus.work-pipeline"},
    )

    first = system_health.remediate_findings(
        [finding], conn=conn, enabled=True, cooldown_seconds=3600, runner=runner)
    second = system_health.remediate_findings(
        [finding], conn=conn, enabled=True, cooldown_seconds=3600, runner=runner)

    assert first == ["com.argus.work-pipeline"]
    assert second == []  # cooldown active, no second kickstart
    assert len(calls) == 1


def test_keepalive_service_labels_reads_plists(tmp_path):
    import plistlib
    def _plist(name, body):
        (tmp_path / f"{name}.plist").write_bytes(plistlib.dumps(body))
    _plist("com.argus.serve", {"Label": "com.argus.serve", "KeepAlive": True})
    _plist("com.argus.postgres", {"Label": "x", "KeepAlive": {"SuccessfulExit": False}})
    _plist("com.argus.poll", {"Label": "x", "StartInterval": 300})  # periodic
    _plist("com.other.thing", {"KeepAlive": True})  # not com.argus.*

    labels = system_health.keepalive_service_labels(tmp_path)

    assert labels == {"com.argus.serve", "com.argus.postgres"}


def test_notify_findings_creates_general_action_with_cooldown(conn, tmp_path, monkeypatch):
    path = _cfg_path(tmp_path)
    monkeypatch.setenv("ARGUS_TEST_SUPPORT_TOKEN", "ok")
    cfg = loader.load(path)
    finding = system_health.Finding(
        severity="error",
        fingerprint="missing-env:ARGUS_TEST_SUPPORT_TOKEN",
        message="missing env key(s): ARGUS_TEST_SUPPORT_TOKEN",
    )

    inserted = system_health.notify_findings(conn, cfg, [finding], cooldown_seconds=3600)
    repeated = system_health.notify_findings(conn, cfg, [finding], cooldown_seconds=3600)
    conn.commit()

    assert inserted == 1
    assert repeated == 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT team_id, destination_ref, payload->>'text' "
            "FROM actions WHERE idempotency_key LIKE 'system_health:%'"
        )
        row = cur.fetchone()
    assert row[0] == "general"
    assert row[1] == "fake:general"
    assert "Argus health: system issue detected" in row[2]
    assert "ARGUS_TEST_SUPPORT_TOKEN" in row[2]


def test_notify_findings_suppresses_duplicate_disk_low_outbox(
    conn, tmp_path, monkeypatch
):
    path = _cfg_path(tmp_path)
    monkeypatch.setenv("ARGUS_TEST_SUPPORT_TOKEN", "ok")
    cfg = loader.load(path)
    finding = system_health.Finding(
        severity="warn",
        fingerprint="disk:low:home",
        message="low disk space under /tmp: 4.0 GB free",
        payload={
            "path": "/tmp",
            "free_gb": 4.0,
            "updated_at": "2026-07-03T08:30:00Z",
        },
    )

    first = system_health.notify_findings(conn, cfg, [finding], cooldown_seconds=0)
    duplicate = system_health.notify_findings(conn, cfg, [finding], cooldown_seconds=0)
    conn.commit()

    assert first == 1
    assert duplicate == 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM actions WHERE type='notify' "
            "AND payload->'findings'->0->>'fingerprint'='disk:low:home'"
        )
        action_count = cur.fetchone()[0]
        cur.execute(
            "SELECT message, channel, payload->>'destination' "
            "FROM alerts WHERE fingerprint LIKE 'disk:low:notify-suppressed:%'"
        )
        suppression = cur.fetchone()
    assert action_count == 1
    assert suppression == (
        "suppressed duplicate low disk notification",
        "log",
        "fake:general",
    )


def test_notify_findings_keeps_new_disk_low_evidence(conn, tmp_path, monkeypatch):
    path = _cfg_path(tmp_path)
    monkeypatch.setenv("ARGUS_TEST_SUPPORT_TOKEN", "ok")
    cfg = loader.load(path)
    first = system_health.Finding(
        severity="warn",
        fingerprint="disk:low:home",
        message="low disk space under /tmp: 4.0 GB free",
        payload={
            "path": "/tmp",
            "free_gb": 4.0,
            "updated_at": "2026-07-03T08:30:00Z",
        },
    )
    newer = system_health.Finding(
        severity="warn",
        fingerprint="disk:low:home",
        message="low disk space under /tmp: 3.9 GB free",
        payload={
            "path": "/tmp",
            "free_gb": 3.9,
            "updated_at": "2026-07-03T08:35:00Z",
        },
    )

    assert system_health.notify_findings(conn, cfg, [first], cooldown_seconds=0) == 1
    assert system_health.notify_findings(conn, cfg, [newer], cooldown_seconds=0) == 1
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM actions WHERE type='notify' "
            "AND payload->'findings'->0->>'fingerprint'='disk:low:home'"
        )
        action_count = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM alerts "
            "WHERE fingerprint LIKE 'disk:low:notify-suppressed:%'"
        )
        suppression_count = cur.fetchone()[0]
    assert action_count == 2
    assert suppression_count == 0


def test_notify_findings_escalates_third_same_day_disk_low(conn, tmp_path, monkeypatch):
    path = _cfg_path(tmp_path)
    monkeypatch.setenv("ARGUS_TEST_SUPPORT_TOKEN", "ok")
    cfg = loader.load(path)
    fingerprint = "disk:low:home"
    for _ in range(2):
        alerts.record(
            conn,
            severity="warn",
            project="general",
            fingerprint=fingerprint,
            message="low disk space under /tmp: 4.0 GB free",
            channel="whatsapp",
        )

    inserted = system_health.notify_findings(
        conn,
        cfg,
        [system_health.Finding(
            severity="warn",
            fingerprint=fingerprint,
            message="low disk space under /tmp: 3.9 GB free",
            payload={"path": "/tmp", "free_gb": 3.9, "updated_at": "2026-07-03T08:30:00Z"},
        )],
        cooldown_seconds=0,
    )
    conn.commit()

    assert inserted == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT severity, message, payload->>'same_day_occurrences' "
            "FROM alerts WHERE fingerprint=%s",
            (f"{fingerprint}:repeated",),
        )
        alert_row = cur.fetchone()
        cur.execute("SELECT payload->'findings'->0->>'fingerprint' FROM actions")
        action_fingerprint = cur.fetchone()[0]
    assert alert_row == ("error", "low disk space under /tmp: 3.9 GB free (repeated 3 times today)", "3")
    assert action_fingerprint == f"{fingerprint}:repeated"


def test_health_followup_fix_it_opens_context_request(conn, tmp_path, monkeypatch):
    path = tmp_path / "argus.yaml"
    path.write_text(
        "company:\n"
        "  name: c\n"
        "  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: general\n"
        "    project: { repo: /tmp/argus, base_branch: main, test_cmd: 'true' }\n"
        "    roles:\n"
        "      - { name: manager, kind: front, prompt: m, engine: { engine: scripted } }\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior] }\n"
        "    channels: [ { type: fake, role: control, channel_id: general } ]\n",
        encoding="utf-8",
    )
    cfg = loader.load(path)
    finding = system_health.Finding(
        severity="error",
        fingerprint="launchd:com.argus.context-distill:1",
        message="launchd job unhealthy: com.argus.context-distill status=1",
    )
    assert system_health.notify_findings(conn, cfg, [finding], cooldown_seconds=0) == 1
    events.ingest_message(
        conn,
        cfg,
        team="general",
        source="fake:general",
        dedup_key="fix-health",
        text="fix it",
        conversation_key="fake:general",
    )
    conn.commit()

    reconcile.route_events(conn, cfg)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE kind='converse'")
        converse_jobs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM requests WHERE team_id='general'")
        request_count = cur.fetchone()[0]
        cur.execute("SELECT payload->>'text' FROM events WHERE dedup_key='fix-health'")
        task = cur.fetchone()[0]
        cur.execute("SELECT payload->>'text' FROM actions WHERE type='reply'")
        reply = cur.fetchone()[0]
        cur.execute("SELECT status FROM conversation_contexts "
                    "WHERE context_type='system_health'")
        context_status = cur.fetchone()[0]

    assert converse_jobs == 0
    assert request_count == 1
    assert "Owner asked to fix latest Argus health issue" in task
    assert "com.argus.context-distill" in task
    assert "On it" in reply
    assert context_status == "resolved"
