import json
from pathlib import Path

from argus.v2.config import loader
from argus.v2.pm import pending


def test_pending_prs_filters_by_v2_branch_prefix(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def runner(argv, cwd):
        assert argv[:3] == ["gh", "pr", "list"]
        assert cwd == repo
        return json.dumps([
            {
                "number": 7,
                "title": "Fix course bug",
                "url": "https://github.test/pull/7",
                "isDraft": True,
                "headRefName": "argus/courses/abc",
                "createdAt": "2026-06-17T00:00:00Z",
                "body": "## Summary\nFixed checkout copy.\n\n## Verification\nQA: pass",
            },
            {
                "number": 8,
                "title": "Wrong prefix",
                "url": "https://github.test/pull/8",
                "isDraft": True,
                "headRefName": "argus/tadam/abc",
                "createdAt": "2026-06-17T00:00:00Z",
                "body": "",
            },
        ])

    rows = pending.pending_prs("courses-clubs", repo, "argus/courses", runner=runner)

    assert [row.number for row in rows] == [7]
    assert rows[0].body.startswith("## Summary")


def test_pending_patches_no_longer_reads_legacy_pm_registry(tmp_path):
    pdir = tmp_path / "pm" / "dev"
    pdir.mkdir(parents=True)
    (pdir / "one.json").write_text(json.dumps({"fingerprint": "F1"}), encoding="utf-8")

    assert pending.pending_patches("dev", tmp_path) == []


def test_render_digest_counts_prs_and_patches():
    text = pending.render_digest(
        "dev",
        [pending.PendingPr("dev", 3, "Fix", "https://github.test/pull/3", False, "")],
        [pending.PendingPatch("dev", "F1", "/tmp/fix.patch", "approve", "pass")],
    )

    assert "1 open PR(s), 1 patch(es)" in text
    assert "PR #3: Fix" in text
    assert "Manual QA follow-up: confirm the deployed fix with owner/manual QA." in text
    assert "patch F1 (senior:approve qa:pass)" in text


def test_clean_drafts_marks_passing_ready_and_closes_failing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        f"    project: {{ repo: {repo}, work_branch_prefix: argus/dev }}\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)
    calls = []

    def runner(argv, cwd):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([
                {"number": 1, "title": "Pass", "url": "u1", "isDraft": True,
                 "headRefName": "argus/dev/one", "createdAt": "", "body": ""},
                {"number": 2, "title": "Fail", "url": "u2", "isDraft": True,
                 "headRefName": "argus/dev/two", "createdAt": "", "body": ""},
            ])
        if argv[:3] == ["gh", "pr", "checks"]:
            return "true\n" if argv[3] == "1" else "false\n"
        return ""

    results = pending.clean_drafts(cfg, ["dev"], runner=runner)

    assert [(r.number, r.action) for r in results] == [(1, "ready"), (2, "closed")]
    assert ["gh", "pr", "ready", "1"] in calls
    assert ["gh", "pr", "close", "2"] in calls


def test_notify_digests_inserts_control_channel_action(tmp_path, conn):
    cfg_path = tmp_path / "argus.yaml"
    cfg_path.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n"
        "  - name: dev\n"
        "    project: { repo: /tmp/dev, work_branch_prefix: argus/dev }\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels:\n"
        "      - { type: cli, role: control, channel_id: local }\n"
        "      - { type: fake, role: control, channel_id: chatA }\n",
        encoding="utf-8",
    )
    cfg = loader.load(cfg_path)

    inserted = pending.notify_digests(
        conn,
        cfg,
        [pending.ProjectDigest("dev", "hello", prs=1, patches=0)],
    )
    conn.commit()

    assert inserted == 1
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, type, destination_ref, payload->>'text' FROM actions")
        assert cur.fetchone() == ("dev", "notify", "fake:chatA", "hello")
