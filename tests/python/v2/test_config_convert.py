from argus.v2.config import convert, loader


def test_convert_legacy_project_dirs_to_v2_config(tmp_path):
    legacy = tmp_path / "argus.config.yaml"
    legacy.write_text("engine:\n  default: echo\n", encoding="utf-8")
    project = tmp_path / "projects" / "luma"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        "name: luma\n"
        "repo: /repo/luma\n"
        "autofix: { mode: propose-only, draft: true }\n"
        "pm: { daily_limit: 2, max_rework_attempts: 1 }\n",
        encoding="utf-8",
    )

    data = convert.convert_file(legacy, project_dirs=[tmp_path / "projects"])
    out = tmp_path / "argus.yaml"
    out.write_text(convert.dumps(data), encoding="utf-8")
    cfg = loader.load(out)

    assert cfg.company.defaults.engine.engine == "echo"
    assert cfg.teams[0].name == "luma"
    assert cfg.teams[0].project.repo == "/repo/luma"
    assert cfg.teams[0].project.autofix.draft is True
    assert cfg.teams[0].project.pm.daily_limit == 2
    roles = {role.name: role for role in cfg.teams[0].roles}
    assert "QA-sensitive work cannot close" in roles["qa"].prompt
    assert "transcript documents" in roles["qa"].prompt
    assert "access path" in roles["qa"].prompt
    assert "item-by-item disposition" in roles["qa"].prompt
    assert "verification coverage" in roles["qa"].prompt
    assert "post-fix follow-up condition" in roles["qa"].prompt
    assert "code regression" in roles["qa"].prompt
    assert "environment blocker" in roles["qa"].prompt
    assert "expected cancellation" in roles["qa"].prompt
    assert "stale status" in roles["qa"].prompt
    assert "unknown" in roles["qa"].prompt
    assert "QA-sensitive work cannot close" in roles["senior"].prompt
    assert "transcript documents" in roles["senior"].prompt
    assert "access path" in roles["senior"].prompt
    assert "item-by-item disposition" in roles["senior"].prompt
    assert "verification coverage" in roles["senior"].prompt
    assert "post-fix follow-up condition" in roles["senior"].prompt
    assert "failing PR summary" in roles["senior"].prompt
    assert "code regression" in roles["senior"].prompt
    assert "environment blocker" in roles["senior"].prompt
    assert "expected cancellation" in roles["senior"].prompt
    assert "stale status" in roles["senior"].prompt
    assert "unknown" in roles["senior"].prompt


def test_convert_without_projects_creates_default_team():
    data = convert.convert({"engine": {"default": "claude-code"}}, project_dirs=[])

    assert data["company"]["defaults"]["engine"] == {"engine": "claude-code"}
    assert data["teams"][0]["name"] == "dev"
