# Argus Agent Guide

> Canonical agent instruction file for this repo. `CLAUDE.md` is a symlink to it, so Claude Code and Codex read the same rules. Personal defaults live in `~/.agents/OPERATING.md`.

Use this file when working on Argus with Codex, Claude Code, or another coding
agent.

For a user-facing install, use
[`skills/argus-live-onboarding/SKILL.md`](skills/argus-live-onboarding/SKILL.md)
as the installer playbook. It tells the agent to inspect the computer first,
ask only for missing decisions or secret locations, and prove `doctor --deep`
plus `go-live` before calling the install operational.

## Product Shape

- Python package: `argus-agent`
- Installed CLI: `argus`
- Main product code: `src/argus/v2`
- Agent engine adapters: `src/argus/engine`
- Optional dashboard: `dashboard`
- Runtime database: Postgres with pgvector
- Default safe engine: `echo`

Do not start with a live LLM engine. Prove local runtime with `echo` first.

## Fresh Source Checkout

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Python must be 3.11 or newer. On macOS, `/usr/bin/python3` may be too old.

## Local Postgres

```bash
docker compose up -d postgres
until docker compose exec -T postgres pg_isready -U argus -d argus; do sleep 1; done

export ARGUS_DB_DSN="host=127.0.0.1 port=5440 dbname=argus user=argus password=argus"
export ARGUS_RUN_ROOT="$PWD/run"
```

## First Runtime Smoke

```bash
argus init --config argus.yaml --force
export ARGUS_CONFIG="$PWD/argus.yaml"
export ARGUS_CONFIG_V2="$PWD/argus.yaml"
export ARGUS_RUN_ROOT="$PWD/run"

argus db migrate
argus validate
argus validate-roles
argus doctor

argus submit --team demo "Review this repo and propose one small improvement"
argus up --iterations 1
argus status
```

Expected `status`: processed `events` and completed `actions`. `requests` and
`jobs` may be `none` in the echo smoke.

## Daily Learning And Retro

`argus retro run` is the daily team and company learning heartbeat. Generated
launchd units run it every 86400 seconds.

Roles:

- Team Learning Agent: per-project Retro Facilitator.
- Company Learning Agent: company Chief of Staff.

Config:

```yaml
retro:
  authority: propose        # propose | auto-changes
  company_change_team: dev  # optional target for company-level auto-change PR work
```

Rules:

- `propose`: writes backlog, bridges safe team lessons into PM memory, and
  bridges company lessons into company knowledge.
- `auto-changes`: may open internal PM requests for evidence-backed `skill`,
  `prompt-edit`, or `process-edit` candidates.
- `retro run` queues PM digests to each project team's control channel and a
  CEO retro brief to the `ceo-brief` control channel unless `--no-notify` is set.
- Auto-changes never merge, deploy, send outward messages, edit secrets, or run
  destructive work directly. Existing action approval gates still apply.
- Unsafe candidates are quarantined by the retro scanner.

Useful commands:

```bash
argus retro run
argus retro run --team dev
argus retro run --company-only
argus retro notify --team dev
argus retro backlog --team __company__
argus retro summary
```

See `docs/retro.md`.

## Guided Project Onboarding

For a real repo, do not stop at `argus init`. Generate project-specific private
artifacts:

```bash
argus onboard project /absolute/path/to/project \
  --mode chat-only \
  --config /absolute/path/to/private/argus.yaml \
  --out-dir /absolute/path/to/private/onboarding \
  --channel slack \
  --channel-id C1234567890
```

Modes:

- `chat-only`: Slack or Telegram manager chat, no connector polling required.
- `monitor-only`: connector polling and notification proof, no PR work.
- `pm-propose-pr`: draft PR workflow with daily cap and approval-safe defaults.

Generated files:

- `argus.yaml`: private runtime config. Keep it out of git.
- `argus.env.example.generated`: required env names only, no values.
- `argus.onboarding.md`: repo-specific checklist and skipped connector notes.

Then prove operation:

```bash
argus doctor --deep --json
argus go-live --mode chat-only --public-url https://argus.example.com/slack
```

Use `--dev-tunnel` only for quick tunnel smoke tests. A real live install needs
a stable webhook URL.

## Codex Or Claude Code Engine

After the echo smoke passes, edit `argus.yaml`:

```yaml
company:
  defaults:
    engine:
      engine: codex
```

Use `engine: claude-code` for Claude Code. Then run:

```bash
argus doctor --live
```

Keep model and tool settings in config only when needed. Keep secrets in env
files, not YAML.

Use `ARGUS_ENV_FILES=/absolute/path/to/argus.env` for private env loading. If
you source an env file manually, run `set -a` first and `set +a` after so child
processes receive `ARGUS_DB_DSN` and secret refs. Vercel connector sources may
use `secret_ref: ${env:VERCEL_TOKEN}`; on local Macs, omitting `secret_ref`
lets Argus use and refresh `vercel login` CLI auth.
Sentry connector sources need `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, and
`SENTRY_PROJECT`; a DSN only proves app instrumentation. PostHog connector
sources need `POSTHOG_PERSONAL_API_KEY`, `POSTHOG_PROJECT_ID`, and
`POSTHOG_HOST`; `NEXT_PUBLIC_POSTHOG_KEY` is not enough for Argus polling.
When a provider is required, run `doctor --deep` and `go-live` with
`--require-source-type sentry` or `--require-source-type posthog`; absent
sources must block `operational` status.
When one project needs coverage, use `--require-team-source-type team:type`.
When every configured project needs coverage, use
`--require-each-team-source-type sentry` and
`--require-each-team-source-type posthog`.

## Slack Or Telegram

Use generated config first:

```bash
argus init --config argus.yaml --force --channel slack
argus init --config argus.yaml --force --channel telegram
```

Required env names:

- Slack: `ARGUS_WEBHOOK_SECRET`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`
- Telegram: `ARGUS_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`

See `docs/inbound.md` for payload examples and live webhook setup.
For full Slack setup, use `docs/slack-live.md`.
For the smoke-to-live path, including manager roles, continuous workers, PM
auto-fix, launchd/systemd, and stable webhook URLs, use
`docs/live-onboarding.md`.

## Browser Verification

Optional `browser_verify` judge stage that checks UI changes in a real browser
against a preview of the change. Off by default. Enable per team by adding a
`browser_verify` role, putting it in `pipeline.stages` between `qa` and `senior`,
and setting `project.browser_verify.enabled: true`. Gated on the diff: it runs
only when a UI file changes (`*.vue`, `*.tsx`, `*.jsx`, `*.svelte`, `*.css`,
`*.scss`); backend-only diffs auto-pass. Fail-closed: any error is a fail, which
keeps the PR draft. Full design in `docs/browser-verify-design.md`.

Backend (`browser_verify.backend`):

- `hermes` (default): drives the browser via the hermes `browser` toolset on the
  `openai-codex` provider (the Codex subscription, `gpt-5.5`). No browser-use, no
  metered LLM key.
- `browser-use`: the browser-use library. Needs a metered LLM key and runs in a
  dedicated venv via `browser_venv_python`.

Preview discovery (`browser_verify.discovery`):

- `vercel` (default): push the branch, poll the Vercel API. Needs
  `vercel_project_id`, `vercel_team_id`, and `VERCEL_TOKEN` in the environment.
- `firebase`: build the site in the worktree and deploy a Firebase Hosting
  preview channel (`firebase_project` + a working `firebase` login). For repos
  whose preview is PR-triggered rather than built on branch push.

Code: `src/argus/v2/browser` (discovery + runner), `_run_browser_verify` in
`src/argus/v2/worker/worker.py`, and the `_is_browser_verify` branch in
`src/argus/v2/orchestrator/pipeline.py`. Verdict is fail-closed and reuses the
existing judge to `force_draft_on_fail` machinery.

## Codebase Memory MCP

`codebase-memory-mcp` is optional project-local code intelligence. Configure it
through `teams[].mcp.servers` with a small read/search allowlist. Do not use its
auto-installer to rewrite agent configs from inside Argus work. Treat graph
answers as pointers only: use them to find files, call paths, and impact areas,
then read source and tests before making claims or edits.

## Verification

Python gate:

```bash
python scripts/gate.py
```

Docs/package checks:

```bash
python -m pytest tests/python/test_no_u2014.py tests/python/test_package.py -q
```

Dashboard checks, only if `dashboard` changed:

```bash
cd dashboard
npm ci
npm run test
npm run build
```

## Editing Rules

- Keep secrets out of committed files.
- Keep runtime config local. `argus.yaml` is ignored by git.
- Prefer smallest change that fixes the real flow.
- Update tests for parser, validation, CLI, or channel behavior changes.
- Public docs must not use em dashes. Use hyphens.
- If CI fails before runner steps with billing/spending-limit annotations, it is
  infrastructure, not product failure. Still run local gate before merging.

## Claude Code specifics

guide for Claude Code, Codex, and other coding agents.

For daily learning work, use `docs/retro.md` as the product contract.
`argus retro run` owns team and company learning. When `retro.authority` is
`auto-changes`, it can open internal PM requests, but must still respect
approval gates for merge, deploy, outward messages, secrets, and destructive
work. It also queues PM digests to project control channels and a CEO retro
brief to the `ceo-brief` control channel unless run with `--no-notify`.

For the pre-merge browser check of UI changes (the `browser_verify` stage,
Vercel or Firebase preview, driven on the Codex subscription via hermes), see
the Browser Verification section in this file and `docs/browser-verify-design.md`.
