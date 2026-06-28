import subprocess

from argus.v2 import system_health
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
