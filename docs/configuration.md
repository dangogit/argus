# Configuration

The v2 config is a typed YAML file loaded from `ARGUS_CONFIG` or
`ARGUS_CONFIG_V2`.

Use this page before connecting real engines, GitHub repos, support inboxes, or
notification channels. The public examples should teach a new operator what to
configure, what to keep private, and what to validate before Argus runs
continuously.

## Onboarding Path

1. Start with the local quickstart and keep `engine: echo` until `argus doctor`
   passes.
2. Create a private `argus.yaml` from `argus init` or from
   `argus.v2.example.yaml`.
3. Set `company.defaults` first. This is the global owner policy for every
   team.
4. Add one team with a real repo, branch policy, role pipeline, test command,
   and control channel.
5. Copy `.env.example` to a private env file, then put database credentials,
   API tokens, webhook secrets, and machine paths there.
6. Run `argus validate`, `argus validate-roles`, `argus projects list`, and
   `argus doctor` before enabling polling, support, or always-on jobs.
7. Add real engines and connectors only after the echo loop works.

Minimal shape:

```yaml
company:
  name: mycompany
  defaults:
    engine: { engine: echo }
    notifications:
      timezone: UTC
      quiet_hours: "22:30-08:30"
      quiet_hours_delivery: hold
      urgent_severities: [error, critical, urgent, emergency, page, wake]
    pipeline: { stages: [developer, qa], max_iters: 2 }
    project:
      autofix: { mode: propose-pr, draft: false, force_draft_on_fail: false }
      pm: { daily_limit: 3 }
retro:
  authority: propose
teams:
  - name: dev
    project:
      repo: .
      base_branch: main
    roles:
      - { name: developer, kind: builder, prompt: "Implement the change." }
      - { name: qa, kind: judge, prompt: "Run checks." }
    channels:
      - { type: cli, role: control, channel_id: local }
```

## What Goes Where

| Layer | Put here | Keep out |
|---|---|---|
| `company.defaults` | Global owner policy: default engine, autonomy, quiet hours, urgency, default pipeline, PM limits, autofix posture, support mode, and shared escalation terms. | Repo paths, private channel IDs that only one team uses, and secrets. |
| `company.sources` | Shared signal sources used across teams, such as Sentry, GitHub, Postgres, uptime checks, or webhooks. | API tokens and transport secrets. Reference them through env names. |
| `retro` | Daily learning authority and optional company auto-change target team. | Secrets, raw incident logs, or project-specific role prompts. |
| `teams[].project` | Repo path, `github_repo`, branch names, worktree prefix, setup command, test command, connector project IDs, and project-specific PM limits. | Owner-wide defaults that every team should inherit. |
| `teams[].ownership` | Explicit team responsibility, code release proof, low-risk support policy, and evidence-backed maintenance limits. | Secrets, production credentials, or broad authority that cannot be scoped to one team. |
| `teams[].roles` | Pipeline role names, role kind, and concise prompts or prompt files in config-directory mode. | Runtime secrets, large knowledge dumps, and temporary incident context. |
| `teams[].channels` | Control, escalation, owner, or publishing channel destinations for that team. | Global notification policy and credentials for those transports. |
| `.env` or `ARGUS_ENV_FILES` | `ARGUS_DB_DSN`, model API keys, GitHub tokens, Apps Script keys, webhook secrets, dashboard token, executable paths, and deploy-only values. | Product behavior that should be reviewable in YAML. |
| Process env | Emergency or host-specific overrides, for example `ARGUS_NOTIFY_TIMEZONE` or `ARGUS_NOTIFY_QUIET_HOURS`. | Long-lived team policy that should live in YAML. |

Use YAML for product behavior and routing policy: engines, autonomy, quiet
hours, notification urgency, sources, channels, and team/project settings. Use
environment variables or env files for secrets and machine-specific runtime
paths: API keys, database DSNs, webhook secrets, executable paths, and
deployment-only overrides.

## Precedence

Config precedence for notifications is:

1. Typed defaults
2. `company.defaults.notifications`
3. `team.notifications`
4. Process env overrides, currently `ARGUS_NOTIFY_QUIET_HOURS` and
   `ARGUS_NOTIFY_TIMEZONE`

Project, pipeline, and support config use the same shape:

1. Typed defaults
2. `company.defaults.project`, `company.defaults.pipeline`, or
   `company.defaults.support`
3. Team or source-specific fields
4. Process env only for runtime overrides and secrets

`ARGUS_CONFIG` wins over `ARGUS_CONFIG_V2` when both are set. Keep both set in
host manifests for compatibility, but treat `ARGUS_CONFIG` as the primary name.

## Global Defaults

Set these once in `company.defaults` unless a team truly needs an override:

- `engine`: use `echo` for the first smoke test, then switch to `codex`,
  `claude-code`, or `hermes` for real agent work.
- `autonomy`: keep `irreversible_outward: approval` for merges, production
  deploys, customer messages, publishing, and calendar writes.
- `notifications`: set the owner-local timezone, quiet hours, delivery mode,
  and urgent severities. Low-priority control notifications can be held during
  quiet hours.
- `pipeline`: define the normal role sequence and `max_iters`.
- `project.autofix`: default to propose-pr behavior for open-source safety.
- `project.pm`: set daily caps so a noisy connector cannot flood maintainers.
- `support`: default to propose mode, set daily limits, and define shared
  escalation terms.

## Retro Learning

`retro` is top-level config because it coordinates all teams.

```yaml
retro:
  authority: propose        # propose | auto-changes
  company_change_team: dev  # optional target for company-level auto-change PR work
```

Use `propose` first. It writes backlog items, bridges team lessons into PM
memory, and bridges company lessons into company knowledge.

Use `auto-changes` only after PM and PR flow are proven. It can open internal PM
requests for evidence-backed `skill`, `prompt-edit`, or `process-edit`
candidates. It still cannot merge, deploy, send outward messages, edit secrets,
or run destructive work directly.

If unsure, keep `engine: echo`, `autofix.mode: propose-pr`,
`irreversible_outward: approval`, `support.mode: propose`, and quiet hours
enabled.

## Team And Project Config

Keep repo identity in team/project config:

- `repo`: local checkout path for the project.
- `github_repo`: GitHub owner and repo when PR creation is enabled.
- `base_branch` and `work_branch_prefix`: branch policy for agent work.
- `setup_cmd` and `test_cmd`: commands workers run before proposing a change.
- `test_timeout_seconds`: timeout for QA `test_cmd` (default `900`).
- connector project IDs and source names.
- support email addresses and source-specific transport config.
- `channels`: where owner-facing notifications should go for that team.

Use team overrides sparingly. A team should override `notifications`,
`pipeline`, `project`, or `support` only when it has a real operational
difference.

## Persistent Ownership

Ownership is a team-level, default-off policy. It keeps an obligation open
after a request or action finishes until the configured real-world result is
proven. Start with approval-gated shadow mode, then add staging authority one
boundary at a time.

The important switches are:

- `ownership.enabled`, default `false`;
- `ownership.code.auto_ready` and `auto_merge`, both default `false`;
- `ownership.code.allowed_base_branches`, `required_checks`,
  `deploy_provider`, provider-specific deployment identity, `live_url`, and
  `smoke_paths`;
- `ownership.support.enabled`, default `true`, and
  `ownership.support.auto_send_low_risk`, default `false`, plus
  `min_confidence` and additive `blocked_categories`;
- `ownership.maintenance.enabled`, default `false`, plus `interval_hours` and
  `max_open`;
- explicit `autonomy.actions` entries for `ready_pr`, `merge_pr`, and
  `support_reply`.

Automatic merge rejects `main`, `master`, `production`, and `prod` and requires
at least one required check. GitHub deployment uses `deploy_workflow`. Vercel
deployment uses `deploy_project` and `deploy_scope`. Both require a live URL.
Vercel authentication defaults to `deploy_vercel_auth: environment`; use `cli`
when the owner host should use its local Vercel CLI login.
Mandatory blocked code paths and support categories are always retained when
custom lists are added.

Use the staging-first YAML and full gate reference in [Persistent Team
Ownership](ownership.md). Then verify the loaded policy without exposing
secrets:

```bash
argus owner prove --team TEAM --json
```

`doctor --deep` proves dependencies and connector access. `owner prove` is a
separate, read-only proof of ownership policy wiring. Run both before enabling
the owner timer.

## MCP Servers

Argus can pass operator-configured MCP servers to MCP-aware engines. Configure
MCP servers in YAML, keep secrets in env vars, and let `argus doctor` validate
that commands and env names exist before live work starts.
Today this is wired through the Claude Code worker path; engines without MCP
support ignore the config.

Use top-level `mcp.servers` only for tools every team may use. Prefer
`teams[].mcp.servers` for project-local tools such as
`codebase-memory-mcp`, so one repo graph is not exposed to unrelated projects.

Example project-local codebase memory:

```yaml
teams:
  - name: dev
    project:
      repo: /absolute/path/to/project
      base_branch: main
    roles:
      - { name: developer, kind: builder, prompt: "Implement the change." }
      - { name: qa, kind: judge, prompt: "Run checks." }
    pipeline: { stages: [developer, qa] }
    mcp:
      servers:
        - name: codebase-memory
          transport: stdio
          command: codebase-memory-mcp
          tools:
            - search_code
            - get_architecture
            - trace_path
            - detect_changes
```

`tools` is a Claude Code allowlist. Plain names become
`mcp__<server>__<tool>` when Argus launches Claude Code. Leave `tools` empty
only when the server is fully trusted for that team.

For `codebase-memory-mcp`, install the binary as an operator step. Do not let an
agent auto-install or rewrite global agent configs. If using the upstream setup
script, pass `--skip-config` and wire it through `argus.yaml` instead. Keep
`.codebase-memory/` local at first; commit `.codebase-memory/graph.db.zst` only
after graph churn and privacy are acceptable for that repo.

Agent rule: use codebase memory to find likely files, symbols, call paths, and
impact areas. Then read source files and tests before claiming facts or editing.

## Config Directory Mode

Single-file YAML is easiest for one team. Config-directory mode is better once
the repo has multiple teams or long prompts:

```text
config/
  company.yaml
  teams/
    dev/
      team.yaml
      roles/
        developer.md
        qa.md
```

The directory loader compiles the same structure as `argus.v2.example.yaml`.
Keep structured fields in YAML and long role prompts in
`teams/<team>/roles/<role>.md`.

## Secrets And Runtime Env

Secrets should come from environment references or env files loaded by
`ARGUS_ENV_FILES`. Do not put tokens in tracked YAML. Use `.env.example` as the
public template for required and optional runtime variables.

Minimum local env:

```bash
export ARGUS_CONFIG="$PWD/argus.yaml"
export ARGUS_CONFIG_V2="$PWD/argus.yaml"
export ARGUS_DB_DSN="host=127.0.0.1 port=5440 dbname=argus user=argus password=argus"
```

For always-on jobs, put secrets in an operator-owned env file and pass it to the
host renderer:

```bash
export ARGUS_ENV_FILES="/absolute/path/to/argus.env"
argus launchd render --out /tmp/argus-launchd --env-file "$ARGUS_ENV_FILES"
argus host render --os linux --jobs-dir ./jobs --out /tmp/argus-systemd
```

Use absolute paths in env files. Do not rely on shell expansion inside values.

Never log raw DSNs, API keys, webhook secrets, Apps Script keys, dashboard
tokens, cookies, or access tokens. Redact them before pasting logs into issues
or support requests. Rotate credentials when changing hosts or sharing a config
with another maintainer.

Useful commands:

```bash
argus config get company.name
argus config get company.defaults.notifications.timezone
argus projects list
argus validate
argus validate-roles
argus doctor
argus config convert --input argus.config.example.yaml --projects-dir projects --out argus.yaml
```

## Validation Checklist

Before enabling live polling or always-on workers:

1. `argus validate` returns `validate: ok`.
2. `argus validate-roles` returns `validate-roles: ok`.
3. `argus projects list` shows exactly the expected teams.
4. `argus config get company.defaults.autonomy.irreversible_outward` is
   `approval`.
5. `argus config get company.defaults.notifications.timezone` matches the
   maintainer's real timezone.
6. `argus doctor` passes config and database checks.
7. A local `echo` run can submit work, execute one loop, and write status rows.
8. For every ownership-enabled team, `argus owner prove --team TEAM --json`
   returns `"ready": true`.
9. Only then enable real engines, source polling, support inboxes, or host jobs.

## Database

Argus uses Postgres through `ARGUS_DB_DSN`. The knowledge migration requires
pgvector, so local development should use a pgvector-enabled Postgres image or a
Postgres install with the `vector` extension available. `argus db migrate`
loads the SQL migrations packaged with the Python core.

```bash
export ARGUS_DB_DSN="host=127.0.0.1 port=5440 dbname=argus user=argus password=argus"
argus db migrate
```
