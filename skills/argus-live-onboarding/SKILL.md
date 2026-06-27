---
name: argus-live-onboarding
description: Use when installing Argus for a user from source or a package, especially when Codex, Claude Code, or another coding agent is doing setup on the user's computer. The skill inspects local projects and tools first, asks only for missing decisions or secret locations, writes private config, and proves chat-only, monitor-only, or pm-propose-pr operation.
---

# Argus Live Onboarding

Goal: turn a local computer plus one or more repos into a proven Argus install.
Do not stop at generated YAML. End in one of these states:

- `chat-only`: Slack, Telegram, CLI, or fake channel reaches a real manager.
- `monitor-only`: required connectors dry-run cleanly or are explicitly skipped.
- `pm-propose-pr`: PM path can create safe draft PRs with approval gates.

## Ground Rules

- Inspect before asking.
- Ask only what cannot be detected or safely inferred.
- Never read or copy secret values from `.env`. Detect key names only.
- Keep `argus.yaml`, env files, tokens, webhook secrets, and support transport
  keys in private paths outside git.
- Start with `echo`. Switch manager to `codex`, `claude-code`, or `hermes`
  only after local runtime smoke passes.
- Do not enable code-changing PM mode until repo path, base branch, test
  command, GitHub auth, worktree creation, and PR policy are proven.
- Customer support email defaults to propose-only. Never auto-send customer
  email unless user explicitly opts in and policy allows it.
- Stable webhook URL required for real go-live. Quick tunnels are dev smoke only.

## Inspect First

From the Argus checkout or installed environment, collect:

```bash
pwd
git status --short --branch || true
argus --version || python -m argus.v2.cli --version
argus doctor || true
command -v codex || true
command -v claude || true
command -v claude-code || true
command -v hermes || true
command -v gh || true
gh auth status || true
```

For each candidate project repo:

```bash
find "$PROJECT" -maxdepth 3 \( -name AGENTS.md -o -name CLAUDE.md -o -name README.md -o -name package.json -o -name pyproject.toml -o -name vercel.json -o -name firebase.json -o -name .env.example \) -print
git -C "$PROJECT" remote get-url origin || true
git -C "$PROJECT" branch --show-current || true
```

Detect env key names only:

```bash
python - <<'PY'
import os
from pathlib import Path
root = Path(os.environ["PROJECT"])
for path in root.rglob(".env*"):
    if path.is_file() and path.stat().st_size < 200000:
        keys = []
        for raw in path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            keys.append(line.split("=", 1)[0].removeprefix("export ").strip())
        if keys:
            print(path, sorted(set(keys)))
PY
```

Look for provider hints:

- Slack or Telegram channel needs channel id and bot secret.
- GitHub needs `gh auth status` or `GITHUB_TOKEN`.
- Vercel can use `vercel login` locally or `VERCEL_TOKEN`.
- Firebase can use `firebase login` locally or service account env.
- Sentry needs `SENTRY_AUTH_TOKEN`, `SENTRY_ORG`, `SENTRY_PROJECT`.
- PostHog needs `POSTHOG_PERSONAL_API_KEY`, `POSTHOG_PROJECT_ID`,
  `POSTHOG_HOST`.
- Supabase needs URL plus service role key for server-side polling.
- Support email needs Apps Script URL and key, or another implemented transport.
- CEO brief and general health/fix route need a control channel.

## Ask Missing Questions

After inspection, ask one compact set of questions. Do not ask for facts already
detected.

Required decisions:

1. Which project repos should Argus manage?
2. Mode per repo: `chat-only`, `monitor-only`, or `pm-propose-pr`.
3. Channel provider: Slack, Telegram, CLI, or fake.
4. Reuse existing channels or create new channels. If Slack MCP is available and
   user authorizes it, create/reuse channels through MCP.
5. Stable webhook URL, or whether this is only `--dev-tunnel` smoke.
6. Manager engine after echo passes: `codex`, `claude-code`, `hermes`, or stay
   `echo`.
7. Required providers per project: GitHub, Vercel, Firebase, Supabase, Sentry,
   PostHog, Fly, Postgres, OpenAPI, uptime, support email.
8. Support email yes/no per project. If yes, ask where private Apps Script URL
   and key live. Do not ask user to paste secret values into chat.
9. CEO brief yes/no and destination channel.
10. General health/fix group yes/no and destination channel.
11. Service manager: launchd on macOS, systemd on Linux, or manual dev run.

If user says "make best judgment", default:

- one Slack channel per project when Slack is configured
- `chat-only` first
- manager `codex` if `codex` binary exists, otherwise `echo`
- developer `echo` until `pm-propose-pr`
- CEO brief and general health/fix route enabled if a control channel exists
- support email disabled unless transport keys already exist in a private env

## Build Private Artifacts

Run per project:

```bash
argus onboard project "$PROJECT" \
  --mode "$MODE" \
  --config "$PRIVATE_DIR/argus.yaml" \
  --out-dir "$PRIVATE_DIR/onboarding" \
  --channel "$CHANNEL" \
  --channel-id "$CHANNEL_ID"
```

Then update private config only when needed:

- project-specific connector sources
- support role plus `support_apps_script` source
- `general` team for Argus health/fix route
- `ceo-brief` team for daily CEO brief
- launchd/systemd jobs

Private env file should include names only until user fills values:

```bash
ARGUS_CONFIG=/absolute/private/argus.yaml
ARGUS_CONFIG_V2=/absolute/private/argus.yaml
ARGUS_DB_DSN=host=127.0.0.1 port=5440 dbname=argus user=argus password=argus
ARGUS_RUN_ROOT=/absolute/private/argus-run
ARGUS_WEBHOOK_SECRET=
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
TELEGRAM_BOT_TOKEN=
GITHUB_TOKEN=
VERCEL_TOKEN=
SENTRY_AUTH_TOKEN=
SENTRY_ORG=
SENTRY_PROJECT=
POSTHOG_PERSONAL_API_KEY=
POSTHOG_PROJECT_ID=
POSTHOG_HOST=
```

Load env for commands:

```bash
export ARGUS_ENV_FILES=/absolute/private/argus.env
```

## Prove Echo Runtime

```bash
argus db migrate
argus validate
argus validate-roles
argus doctor
argus submit --team demo "hello"
argus up --iterations 1
argus status
```

Expected: processed events and done actions. Requests/jobs may be none in echo
smoke.

## Prove Channel

For Slack or Telegram:

```bash
argus serve --host 127.0.0.1 --port 8787
argus inbound handle --channel slack --secret "$ARGUS_WEBHOOK_SECRET" --file /tmp/payload.json
argus up --iterations 1
argus actions list --limit 10
```

For real Slack, set Request URL to:

```text
https://stable-public-url.example/slack
```

For Telegram, use:

```text
https://stable-public-url.example/telegram
```

## Prove Deep Operation

Use machine-readable checks:

```bash
argus doctor --deep --live --json
```

Require providers that matter:

```bash
argus doctor --deep --live --json \
  --require-team-source-type my-project:sentry \
  --require-team-source-type my-project:posthog
```

Go live:

```bash
argus go-live --mode "$MODE" --public-url "$PUBLIC_URL"
```

For all-team provider requirements:

```bash
argus go-live --mode monitor-only --public-url "$PUBLIC_URL" \
  --require-each-team-source-type sentry \
  --require-each-team-source-type posthog
```

For PM mode, keep PR smoke unless user explicitly skips:

```bash
argus go-live --mode pm-propose-pr --public-url "$PUBLIC_URL"
```

## Install Continuous Jobs

macOS:

```bash
argus launchd render \
  --out /tmp/argus-launchd \
  --env-file "$ARGUS_ENV_FILES" \
  --config "$ARGUS_CONFIG" \
  --run-root "$ARGUS_RUN_ROOT"
```

Install rendered units with `launchctl bootstrap` or project host command if
available. Confirm:

```bash
launchctl list | grep com.argus
argus ready --live
```

Linux: use systemd units with same env and command shape:

- `argus serve --port 8787`
- `argus up --poll 10`
- `argus poll`
- `argus host watchdog`
- optional `argus brief ceo --notify --once-per-day`
- optional `argus support run --team TEAM`

## Done Report

Final report must include:

- installed path and active config path
- private env file path, without values
- managed projects and modes
- channel ids and public URL
- engines per role
- connectors configured, skipped, or blocked
- support email status per project
- CEO brief and general health route status
- launchd/systemd status
- exact `doctor --deep` and `go-live` result
- remaining blockers, if any

Never call install "operational" if any required proof is missing.
