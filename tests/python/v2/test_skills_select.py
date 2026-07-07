"""Skills load-on-relevance: parsing, deterministic bounded selection, render,
and the regression guard that an empty pool injects nothing."""
from pathlib import Path
from types import SimpleNamespace

from argus.v2.skills import registry as sk
from argus.v2.worker import exec as job_exec

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLED_SKILLS = REPO_ROOT / "prompts" / "skills"


def _write(d, name, triggers, body, roles=None):
    fm = f"name: {name}\ntriggers: {triggers}\n"
    if roles is not None:
        fm += f"roles: {roles}\n"
    (d / f"{name}.md").write_text(f"---\n{fm}---\n{body}\n", encoding="utf-8")


def test_parse_frontmatter():
    s = sk.parse_skill("---\nname: x\ntriggers: [a, B]\nroles: [judge]\n---\nbody here",
                       fallback_name="fallback")
    assert s.name == "x"
    assert s.triggers == ("a", "b")          # lowercased
    assert s.roles == ("judge",)
    assert s.body == "body here"


def test_parse_no_frontmatter_uses_fallback_name():
    s = sk.parse_skill("just a body", fallback_name="stem")
    assert s.name == "stem" and s.body == "just a body"


def test_load_skips_underscore_files(tmp_path):
    _write(tmp_path, "real", "[deploy]", "do the deploy")
    (tmp_path / "_README.md").write_text("---\nname: doc\n---\nnot a skill", encoding="utf-8")
    loaded = sk.load_skills(tmp_path)
    assert set(loaded) == {"real"}


def test_select_by_trigger_and_bound(tmp_path):
    _write(tmp_path, "review", "[review, diff]", "review playbook")
    _write(tmp_path, "deploy", "[deploy, ship]", "deploy playbook")
    _write(tmp_path, "noise", "[zzz]", "irrelevant")
    skills = sk.load_skills(tmp_path)
    picked = sk.select(skills, role="judge", text="please review this diff", max_k=2)
    assert [s.name for s in picked] == ["review"]          # only trigger match, bounded


def test_select_respects_role_and_allow(tmp_path):
    _write(tmp_path, "judge_only", "[review]", "x", roles="[judge]")
    _write(tmp_path, "any_role", "[review]", "y")
    skills = sk.load_skills(tmp_path)
    # builder is excluded from judge_only by role scoping
    assert [s.name for s in sk.select(skills, role="builder", text="review")] == ["any_role"]
    # allow-list narrows candidates to the role's declared skills
    assert sk.select(skills, role="judge", text="review", allow=["any_role"]) \
        == [skills["any_role"]]


def test_select_orders_by_hits_then_name(tmp_path):
    _write(tmp_path, "b_one", "[deploy]", "x")
    _write(tmp_path, "a_two", "[deploy, ship]", "y")
    skills = sk.load_skills(tmp_path)
    picked = sk.select(skills, role="w", text="deploy and ship now", max_k=2)
    assert [s.name for s in picked] == ["a_two", "b_one"]  # 2 hits before 1 hit


def test_render_empty_is_empty_string():
    assert sk.render([]) == ""                              # no match => zero tokens


def test_block_for_empty_pool_injects_nothing(tmp_path):
    # regression guard: no skill files => byte-identical prompt to pre-skills
    assert sk.block_for("advisor", "anything at all", dirs=[tmp_path]) == ""


def test_block_for_renders_match(tmp_path):
    _write(tmp_path, "review", "[review]", "REVIEW BODY")
    block = sk.block_for("judge", "please review", dirs=[tmp_path])
    assert "REVIEW BODY" in block and block.startswith("SKILLS")


def test_bundled_minimal_change_skill_matches_code_fix():
    block = sk.block_for(
        "developer",
        "fix failing tests for this regression",
        dirs=[BUNDLED_SKILLS],
        allow=["minimal-change"],
    )
    assert "Make the smallest correct change" in block


def test_bundled_concise_manager_reply_preserves_result_json():
    block = sk.block_for(
        "manager",
        "what is the PR status?",
        dirs=[BUNDLED_SKILLS],
        allow=["concise-manager-reply"],
    )
    assert "Keep the owner-facing reply short" in block
    assert "ARGUS_RESULT" in block


def test_bundled_project_rules_matches_developer_work():
    block = sk.block_for(
        "developer",
        "fix broken auth issue",
        dirs=[BUNDLED_SKILLS],
        allow=["project-rules"],
    )
    assert "AGENTS.md" in block
    assert "QA-sensitive work cannot close" in block
    assert "verification path" in block
    assert "every covered report or item" in block
    assert "post-fix follow-up condition" in block


def test_bundled_support_email_triage_matches_vendor_notice():
    block = sk.block_for(
        "support",
        "We're updating our privacy policy",
        dirs=[BUNDLED_SKILLS],
        allow=["support-email-triage"],
    )
    assert "Vendor notices" in block


def test_build_prompt_appends_frozen_skills():
    # worker consumes the block frozen into exec_snapshot["skills"] at enqueue
    job = SimpleNamespace(
        exec_snapshot={"prompt": "SYS", "skills": "SKILLS (apply when relevant):\n\nBODY"},
        payload={"text": "do it"})
    out = job_exec.build_prompt(job, context="CTX")
    assert out.index("SYS") < out.index("BODY") < out.index("do it")


def test_build_prompt_orders_rules_before_skills():
    job = SimpleNamespace(
        exec_snapshot={
            "prompt": "SYS",
            "rules": "OWNER RULES\n- rule",
            "skills": "SKILLS (apply when relevant):\n\nBODY",
        },
        payload={"text": "do it"})
    out = job_exec.build_prompt(job, context="CTX")
    assert out.index("SYS") < out.index("OWNER RULES") < out.index("BODY") < out.index("do it")


def test_build_prompt_appends_checkpoint_guidance_after_skills():
    job = SimpleNamespace(
        exec_snapshot={
            "prompt": "SYS",
            "skills": "SKILLS (apply when relevant):\n\nBODY",
            "checkpoints": "CHECKPOINTS:\n- report progress",
        },
        payload={"text": "do it"})
    out = job_exec.build_prompt(job, context="CTX")
    assert out.index("BODY") < out.index("CHECKPOINTS") < out.index("do it")


def test_build_prompt_without_skills_key_is_unchanged():
    job = SimpleNamespace(exec_snapshot={"prompt": "SYS"}, payload={"text": "t"})
    assert job_exec.build_prompt(job) == "SYS\n\nTASK:\nt"
