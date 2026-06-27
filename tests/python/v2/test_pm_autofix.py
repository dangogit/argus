import pytest

from argus.v2 import alerts
from argus.v2.pm import autofix


def _project_cfg(tmp_path):
    from argus.v2.config import loader

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        f"    project: {{ repo: {repo}, pm: {{ daily_limit: 2 }} }}\n"
        "    roles:\n"
        "      - { name: developer, kind: builder, prompt: p }\n"
        "      - { name: qa, kind: judge, prompt: p }\n"
        "      - { name: senior, kind: judge, prompt: p }\n"
        "    pipeline: { stages: [developer, qa, senior] }\n",
        encoding="utf-8",
    )
    return loader.load(cfg_path)


def test_dispatch_loads_finding_and_enqueues_first_stage(conn, tmp_path, monkeypatch):
    cfg = _project_cfg(tmp_path)
    alerts.record(
        conn,
        severity="error",
        project="dev",
        fingerprint="F1",
        message="fix login",
    )

    result = autofix.dispatch(conn, cfg, project="dev", fingerprint="F1")
    conn.commit()

    assert result.status == "queued"
    assert result.request_id is not None
    with conn.cursor() as cur:
        cur.execute("SELECT kind, source, payload->>'text' FROM events")
        assert cur.fetchone() == ("signal", "pm:pm", "fix login")
        cur.execute("SELECT role, stage FROM jobs WHERE request_id=%s", (result.request_id,))
        assert cur.fetchone() == ("developer", 0)


def test_dispatch_respects_daily_limit(conn, tmp_path):
    cfg = _project_cfg(tmp_path)

    autofix.dispatch(conn, cfg, project="dev", fingerprint="F1", message="one")
    autofix.dispatch(conn, cfg, project="dev", fingerprint="F2", message="two")
    with pytest.raises(RuntimeError, match="daily cap"):
        autofix.dispatch(conn, cfg, project="dev", fingerprint="F3", message="three")


def test_cycle_filters_actionable_findings(conn, tmp_path, monkeypatch):
    cfg = _project_cfg(tmp_path)
    alerts.record(conn, severity="info", project="dev", fingerprint="I", message="skip")
    alerts.record(conn, severity="warn", project="dev", fingerprint="W", message="do")
    alerts.record(conn, severity="error", project="dev", fingerprint="E", message="do")

    results = autofix.cycle(conn, cfg, project="dev")
    conn.commit()

    assert [r.fingerprint for r in results] == ["W", "E"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM requests")
        assert cur.fetchone()[0] == 2


def test_load_finding_reads_alert_payload(conn):
    assert autofix.load_finding(conn, "dev", "F1") is None
    alerts.record(
        conn,
        severity="warn",
        project="dev",
        fingerprint="F1",
        message="fix by title",
        payload={"title": "fix by title"},
    )

    assert autofix.load_finding(conn, "dev", "F1")["title"] == "fix by title"


def test_dispatch_without_message_or_finding_fails(conn, tmp_path):
    cfg = _project_cfg(tmp_path)

    with pytest.raises(FileNotFoundError):
        autofix.dispatch(conn, cfg, project="dev", fingerprint="missing")


def test_dispatch_duplicate_active_request_is_noop(conn, tmp_path):
    cfg = _project_cfg(tmp_path)

    first = autofix.dispatch(conn, cfg, project="dev", fingerprint="F1", message="one")
    second = autofix.dispatch(conn, cfg, project="dev", fingerprint="F1", message="one")

    assert first.request_id is not None
    assert second.status == "active"
    assert second.request_id is None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pm_dispatches")
        assert cur.fetchone()[0] == 1


def test_dispatch_requires_project_config(conn, tmp_path):
    from argus.v2.config import loader

    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)

    with pytest.raises(ValueError, match="no project config"):
        autofix.dispatch(conn, cfg, project="dev", fingerprint="F1", message="one")


def test_cycle_without_alerts_is_noop(conn, tmp_path):
    cfg = _project_cfg(tmp_path)

    assert autofix.cycle(conn, cfg, project="dev") == []


def test_daily_limit_zero_fails(conn, tmp_path):
    cfg = _project_cfg(tmp_path)
    cfg.team("dev").project.pm.daily_limit = 0

    with pytest.raises(RuntimeError, match="daily cap disabled"):
        autofix.dispatch(conn, cfg, project="dev", fingerprint="F1", message="one")


def test_finding_text_falls_back_to_compact_json():
    assert autofix._finding_text({"fingerprint": "F1", "severity": "warn"}) == (
        '{"fingerprint":"F1","severity":"warn"}'
    )


def test_safe_fingerprint_replaces_unsafe_chars():
    assert autofix.safe_fingerprint("a/b c") == "a-b-c"
