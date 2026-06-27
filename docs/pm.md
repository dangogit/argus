# PM Auto-Fix

The PM loop is a v2 pipeline over the durable core. It scans actionable alerts,
opens a request, runs the configured roles, applies QA and senior-review gates,
scans diffs for secrets, and proposes PRs through the action system.

Commands:

```bash
argus pm run dev FINGERPRINT --message "fix login"
argus pm cycle dev
argus pm pending --notify dev
argus pm clean-drafts dev
```

Project config:

```yaml
project:
  repo: /path/to/repo
  base_branch: main
  test_cmd: "pytest -q"
  autofix:
    mode: propose-pr
    draft: false
    force_draft_on_fail: true
  pm:
    daily_limit: 3
    max_rework_attempts: 2
```

PM memory and retro lessons are stored in Postgres and injected into PM prompts
by project. Company retro lessons are stored in `knowledge` with
`scope='company'`, so all teams can recall them through the normal context path.

When `retro.authority` is `auto-changes`, eligible retro candidates open normal
PM requests with `retro-change:<backlog_id>` fingerprints. They still move
through the same developer, QA, senior, PR, and approval gates as any other PM
work.
