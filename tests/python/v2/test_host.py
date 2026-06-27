import plistlib
import subprocess

from argus.v2 import host


def test_parse_and_render_launchd_job(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "triage.yaml").write_text(
        "name: triage\n"
        "kind: schedule\n"
        "command: argus poll\n"
        "interval: 300\n",
        encoding="utf-8",
    )

    jobs = host.discover_jobs([jobs_dir])
    data = plistlib.loads(host.render_launchd_job(jobs[0]))

    assert jobs == [host.Job(name="triage", kind="schedule", command="argus poll", interval=300)]
    assert data["Label"] == "com.argus.triage"
    assert data["ProgramArguments"] == ["/bin/bash", "-c", "argus poll"]
    assert data["StartInterval"] == 300


def test_launchd_service_and_calendar_rendering():
    service = host.Job(name="up", kind="service", command="argus up")
    calendar = host.Job(name="nightly", kind="schedule", command="argus host backup", calendar="03:30")

    service_data = plistlib.loads(host.render_launchd_job(service))
    calendar_data = plistlib.loads(host.render_launchd_job(calendar))

    assert service_data["KeepAlive"] is True
    assert service_data["RunAtLoad"] is True
    assert calendar_data["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}


def test_discover_jobs_skips_duplicate_names(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one.yaml").write_text("name: same\nkind: service\ncommand: argus up\n", encoding="utf-8")
    (b / "two.yaml").write_text("name: same\nkind: service\ncommand: argus poll\n", encoding="utf-8")

    jobs = host.discover_jobs([a, b])

    assert [job.command for job in jobs] == ["argus up"]


def test_parse_job_rejects_missing_fields(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: bad\n", encoding="utf-8")

    try:
        host.parse_job(path)
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_render_systemd_schedule_writes_service_and_timer(tmp_path):
    job = host.Job(name="nightly", kind="schedule", command="argus host backup", calendar="03:30")

    paths = host.write_jobs([job], tmp_path, os_name="linux")

    assert [path.name for path in paths] == ["argus-nightly.service", "argus-nightly.timer"]
    assert "ExecStart=/bin/bash -c 'argus host backup'" in (tmp_path / "argus-nightly.service").read_text()
    assert "OnCalendar=*-*-* 03:30:00" in (tmp_path / "argus-nightly.timer").read_text()


def test_install_and_uninstall_dry_run_use_install_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_INSTALL_DIR", str(tmp_path / "install"))
    monkeypatch.setenv("ARGUS_HOST_OS", "linux")
    job = host.Job(name="poller", kind="schedule", command="argus poll", interval=60)

    installed = host.install([job], dry_run=True)
    uninstalled = host.uninstall(dry_run=True)

    assert [path.name for path in installed] == ["argus-poller.service", "argus-poller.timer"]
    assert {path.name for path in uninstalled} == {"argus-poller.service", "argus-poller.timer"}


def test_status_reports_systemd_output(monkeypatch):
    monkeypatch.setenv("ARGUS_HOST_OS", "linux")

    def fake_run(argv, capture_output=False, text=False, check=False):
        assert argv[:3] == ["systemctl", "--user", "list-units"]
        return subprocess.CompletedProcess(argv, 0, stdout="argus-poll.service loaded\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert host.status() == "argus-poll.service loaded"


def test_backup_copies_artifacts_and_writes_sums(tmp_path):
    run_root = tmp_path / "run"
    media = run_root / "media"
    media.mkdir(parents=True)
    (media / "a.txt").write_text("artifact", encoding="utf-8")

    dest = host.backup(run_root, tmp_path / "backups")

    assert (dest / "media" / "a.txt").read_text(encoding="utf-8") == "artifact"
    assert "media/a.txt" in (dest / "SHA256SUMS").read_text(encoding="utf-8")


def test_logrotate_rotates_large_logs(tmp_path):
    log = tmp_path / "up.log"
    log.write_text("x" * 10, encoding="utf-8")

    rotated = host.logrotate(tmp_path, max_bytes=5, retain_days=7)

    assert rotated == 1
    assert log.read_text(encoding="utf-8") == ""
    assert list(tmp_path.glob("up.log.*"))
