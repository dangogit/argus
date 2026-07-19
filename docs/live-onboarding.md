# Live Onboarding

This page is the "what next?" path after the echo quickstart. It explains what
counts as live, which processes must run, and when PM auto-fix is actually
enabled.

## What Live Means

A live Argus install has all of these pieces:

1. Postgres is running and migrations are applied.
2. `argus serve` is reachable by the external webhook provider.
3. `argus up --poll 10` is running continuously.
4. Each project has a control channel, usually one Slack channel per team.
5. Each project has a `manager` role with a real engine for project-status chat.
6. PM auto-fix has developer, QA, and optional senior roles plus repo policy.
7. An ownership-enabled project passes its separate policy wiring proof.
8. Always-on jobs, including the owner cycle, are supervised by launchd,
   systemd, or another process manager.
9. Public webhooks use a stable URL, not a disposable quick tunnel.

If only `argus serve` is running, inbound messages land in Postgres but do not
process until `argus up` runs. That is a smoke test, not live operation.

## Agent Prompt

For skill-aware agents, use
[`skills/argus-live-onboarding/SKILL.md`](../skills/argus-live-onboarding/SKILL.md).
That skill is the preferred installer flow because it tells the agent to inspect
the local computer first, ask only for missing decisions or secret locations,
write private config, and prove `doctor --deep` plus `go-live`.

Use this prompt in Codex or Claude Code from the Argus checkout:

```text
Read AGENTS.md, docs/configuration.md, docs/ownership.md, docs/slack-live.md, and
docs/live-onboarding.md. Configure a private argus.yaml and .env.local for one
project first. Keep secrets out of git. Prove echo quickstart, then Slack
inbound, then a continuous argus up worker. Add a manager role with codex or
claude-code only after echo passes. Keep the PM draft-PR path gated by repo,
branch, tests, and PR policy.
```

## Fast Wow Path

For a new project, start with one command:

```bash
argus wow /absolute/path/to/project \
  --out-dir /absolute/path/to/private/argus-wow \
  --channel slack \
  --channel-id C1234567890
```

`argus wow` scans the repo, writes a private `argus.yaml`, writes env names
only, enables the PM draft-PR path, and prints the exact smoke commands. It
sets `allow_code_mode: true` by default, with developer, QA, and senior roles
using code-capable engines. It never copies secret values from `.env`.

Clean installs launched from non-login launchd or SSH environments must set
`PATH` before running `argus doctor`. Include project venv `bin`, Homebrew
`bin`, and directories containing `codex`, `pytest`, and `gh`.

The first PM task is queued only when `ARGUS_DB_DSN` exists, migrations have
run, and the generated config can load with its secrets. Without that, the
command still writes artifacts and prints the `argus submit` command to run
after setup.

For zero-secret local proof, use `--channel cli`. For Telegram or WhatsApp,
use `--channel telegram` or `--channel whatsapp` and fill the generated env
file before webhook smoke.

For WhatsApp over an existing Evolution install, `EVOLUTION_API_KEY` is accepted
as an alias for `ARGUS_WA_APIKEY`. `EVOLUTION_URL` or `EVOLUTION_API_URL` and
`EVOLUTION_INSTANCE` are accepted too, but explicit `ARGUS_WA_*` names are
preferred in new installs.

For clean macOS installs launched from SSH or `launchd`, make sure the runtime
`PATH` includes the project venv `bin`, Homebrew `bin`, and the directories
that contain `codex`, `pytest`, and `gh` before running `argus doctor --deep`.
Non-login shells often start with `/usr/bin:/bin:/usr/sbin:/sbin` only.

Before enabling PM on Argus itself or any repo whose tests use the live
`ARGUS_DB_DSN`, point `test_cmd` at a test database or a narrow smoke command.
Running the whole suite against the live Argus DB can write fixture rows into
the operational database.

## 1. Prove Local Echo

Follow `docs/quickstart.md` first:

```bash
argus db migrate
argus validate
argus validate-roles
argus doctor
argus submit --team demo "hello"
argus up --iterations 1
argus status
```

Expected result: `events` processed and `actions` done. In the echo quickstart,
`requests` and `jobs` may be `none`.

## 2. Guided Project Onboarding

Run the guided repo scanner before editing a large config by hand:

```bash
argus onboard project /absolute/path/to/project \
  --mode chat-only \
  --config /absolute/path/to/private/argus.yaml \
  --out-dir /absolute/path/to/private/onboarding \
  --channel slack \
  --channel-id C1234567890
```

Modes:

- `chat-only`: manager chat over Slack, Telegram, CLI, or fake channel.
- `monitor-only`: connectors must dry-run cleanly or be explicitly skipped.
- `pm-propose-pr`: manager, developer, QA, and senior draft-PR path.

Generated artifacts:

- `argus.yaml`: private Argus config for the repo.
- `argus.env.example.generated`: required env names only.
- `argus.onboarding.md`: repo-specific checklist, detected docs, test command,
  provider hints, and skipped items.

The scanner looks for `AGENTS.md`, `CLAUDE.md`, README files, package files,
CI workflows, `.env.example`, `.env` key names, `vercel.json`, Supabase,
Firebase, Sentry, Git remote, and test commands. It never copies secret values
from `.env`.

For Vercel, local installs may use `vercel login` instead of putting a Vercel
token in Argus env. If a `vercel` source has no `secret_ref`, Argus reads and
refreshes the Vercel CLI auth file. For hosted installs, use a dedicated
`VERCEL_TOKEN`. `vercel` monitors failed deployments. `vercel_events` monitors
production 5xx events from the latest READY deployment and is useful when Sentry
or PostHog API tokens are not available yet.

For Firebase, local installs may use `firebase login` instead of putting a
bearer token in Argus env. If a `firebase` source has no `secret_ref`, Argus
reads and refreshes Firebase CLI auth.

Sentry and PostHog need provider API credentials, not only app-side browser
instrumentation:

```bash
SENTRY_AUTH_TOKEN=sntrys_replace_me
SENTRY_ORG=my-org
SENTRY_PROJECT=my-project
POSTHOG_PERSONAL_API_KEY=phx_replace_me
POSTHOG_PROJECT_ID=12345
POSTHOG_HOST=https://us.posthog.com
```

Example source stubs:

```yaml
sources:
  - type: sentry
    name: sentry-my-project-issues
    secret_ref: ${env:SENTRY_AUTH_TOKEN}
    config:
      org: ${env:SENTRY_ORG}
      project: ${env:SENTRY_PROJECT}
      min_level: error
  - type: posthog
    name: posthog-my-project-errors
    secret_ref: ${env:POSTHOG_PERSONAL_API_KEY}
    config:
      host: ${env:POSTHOG_HOST}
      project: ${env:POSTHOG_PROJECT_ID}
      endpoint: error_tracking
      project_name: my-project
```

`SENTRY_DSN`, `NEXT_PUBLIC_SENTRY_DSN`, and `NEXT_PUBLIC_POSTHOG_KEY` only prove
that the app can emit telemetry. They do not let Argus poll provider APIs, so
`doctor --deep` and `go-live` should still treat those connectors as not
configured until the API credentials above exist.

Then run the deep machine-readable check:

```bash
argus doctor --deep --live --json
```

If Sentry or PostHog is required for your project, make that explicit so Argus
does not call the install live while those sources are absent:

```bash
argus doctor --deep --live --json \
  --require-source-type sentry \
  --require-source-type posthog
```

For one project, gate the team explicitly:

```bash
argus doctor --deep --live --json \
  --require-team-source-type my-project:sentry \
  --require-team-source-type my-project:posthog
```

For every configured project, use the compact all-team gate:

```bash
argus doctor --deep --live --json \
  --require-each-team-source-type sentry \
  --require-each-team-source-type posthog
```

`doctor --deep` validates every configured role engine, channel adapter, repo
path, base branch, `gh` availability, test command executable, and configured
connector dry-run status.

For an ownership-enabled team, also run:

```bash
argus owner prove --team my-project --json
```

`doctor --deep` proves runtime dependencies and live connector access. `owner
prove` is read-only and separately proves explicit action modes, branch and
check policy, staging workflow and smoke target, support transport readiness,
maintenance policy, and current due or blocked work. It exits nonzero while
required ownership wiring is missing.

## 3. Configure Slack Inbound

Follow `docs/slack-live.md` and use one Slack channel per team.

Minimum private env file:

```bash
ARGUS_CONFIG=/absolute/path/to/argus.yaml
ARGUS_CONFIG_V2=/absolute/path/to/argus.yaml
ARGUS_DB_DSN="host=127.0.0.1 port=5440 dbname=argus user=argus password=argus"
ARGUS_RUN_ROOT=/absolute/path/to/argus-run
ARGUS_WEBHOOK_SECRET=replace-with-local-random-secret
SLACK_BOT_TOKEN=xoxb-replace-with-real-token
SLACK_SIGNING_SECRET=replace-with-real-signing-secret
```

Load it for local commands:

```bash
export ARGUS_ENV_FILES="$PWD/.env.local"
```

If you choose to source the file in a shell instead, export its variables:

```bash
set -a
. "$PWD/.env.local"
set +a
```

Start the receiver:

```bash
argus serve --host 127.0.0.1 --port 8787
```

Expose it with a stable tunnel or host. A quick tunnel is fine for a smoke test,
but it is not durable:

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

Slack Event Subscriptions Request URL:

```text
https://your-public-url.example/slack
```

## 4. Start The Worker

For a one-time smoke:

```bash
argus up --iterations 1
```

For live operation:

```bash
argus up --poll 10
```

Keep `serve` and `up` running at the same time. Test from Slack:

```text
@Argus status please
```

Then check:

```bash
argus status
```

If `events` shows `received`, `serve` works but `up` is not processing. If
`actions` stays `proposed`, check channel credentials and action policy.

## 5. Add Project Manager Chat

Without a manager role, Slack messages use the deterministic fallback. It
replies `Got it.` or dispatches only when the message contains a work verb.

Add a manager role with a real engine when you want project-status answers:

```yaml
company:
  defaults:
    engine: { engine: echo }
teams:
  - name: my-project
    project:
      repo: /absolute/path/to/my-project
      base_branch: main
      work_branch_prefix: argus/my-project
      github_repo: owner/repo
    roles:
      - name: manager
        kind: front
        prompt: >-
          You are Argus PM manager for this project. Use ARGUS STATE to answer
          status questions. For code work, dispatch only when the task is
          specific. Last line must be exactly ARGUS_RESULT:
          {"action":"answer|dispatch|ignore","reply":"short Slack reply","task":"specific task or empty"}.
        engine: { engine: codex }
      - { name: developer, kind: builder, prompt: "Make the smallest safe change." }
    pipeline: { stages: [developer], max_iters: 1 }
```

`manager` handles Slack chat. `developer` handles PM work only when a request is
dispatched.

## 6. Enable PM Auto-Fix

PM auto-fix is not enabled just because Slack works. Configure repo policy,
roles, tests, and PR behavior:

```yaml
company:
  defaults:
    autonomy:
      reversible_internal: auto
      personal_outward: approval
      irreversible_outward: approval
    project:
      allow_code_mode: true
      allow_network: true
      autofix: { mode: propose-pr, draft: true, force_draft_on_fail: true }
      pm: { daily_limit: 1, max_rework_attempts: 1 }
teams:
  - name: my-project
    project:
      repo: /absolute/path/to/my-project
      base_branch: main
      work_branch_prefix: argus/my-project
      github_repo: owner/repo
      test_cmd: "pytest -q"
    roles:
      - { name: manager, kind: front, prompt: "PM manager prompt", engine: { engine: codex } }
      - { name: developer, kind: builder, prompt: "Make the smallest safe change.", engine: { engine: codex } }
      - { name: qa, kind: judge, prompt: "Run checks and verify the change.", engine: { engine: codex } }
      - { name: senior, kind: judge, prompt: "Approve only low-risk correct changes.", engine: { engine: codex } }
    pipeline: { stages: [developer, qa, senior], max_iters: 1 }
```

Run one explicit PM task:

```bash
argus pm run my-project manual-smoke --message "Fix the failing login test"
argus status
```

Watch progress:

```bash
argus runs REQUEST_ID
argus actions REQUEST_ID
argus pm pending --notify my-project
```

Keep `draft: true` for the first real project. Move to ready PRs only after the
project has reliable tests and you trust the loop.

### Optional: persistent team ownership

PM can finish a pipeline while the real outcome still waits on a PR, merge,
staging deploy, smoke check, or customer reply. Persistent ownership records
that remaining responsibility as an obligation and reconciles it on a timer.
It is disabled by default.

Follow [Persistent Team Ownership](ownership.md). Start in shadow mode with
ready, merge, and support action overrides set to `approval`. Prove several
cycles before enabling automatic ready on a staging branch. Keep production
merges approval-gated.

## 7. Make It Always-On

macOS launchd:

```bash
export ARGUS_RUN_ROOT="$HOME/argus-run"

argus launchd render \
  --out "$HOME/Library/LaunchAgents" \
  --env-file /absolute/path/to/argus.env \
  --config /absolute/path/to/argus.yaml \
  --run-root "$ARGUS_RUN_ROOT"
```

Load units:

```bash
for f in "$HOME"/Library/LaunchAgents/com.argus.*.plist; do
  launchctl bootstrap "gui/$(id -u)" "$f"
done
```

Verify:

```bash
argus ready --live
argus owner prove --team my-project --json
launchctl list | grep com.argus
```

The built-in bundle includes an `owner` timer that runs `argus owner cycle
--json` every 300 seconds. On Linux, `argus launchd render --os linux` emits the
matching `argus-owner.service` and `argus-owner.timer` units. The generic
manifest-based `argus host render` only includes jobs present in `jobs-dir`.

macOS privacy warning: LaunchAgents may not read files under `Desktop`,
`Documents`, or other protected folders unless the host process has permission.
For unattended operation, put the Argus runtime config, env file, and venv under
a non-protected path such as `$HOME/argus-live`, or grant the needed Full Disk
Access manually. Project repos under protected folders can still block PM
worktrees.

Linux systemd:

```bash
argus host render --os linux --jobs-dir ./jobs --out /tmp/argus-systemd
argus host install --jobs-dir ./jobs --dry-run
```

## 8. Stable Webhook URL

Disposable tunnels are for testing only. For real Slack or Telegram operation,
use one of:

- Cloudflare named tunnel with a stable hostname.
- ngrok reserved domain.
- Real server URL behind HTTPS.

After changing URL, update Slack Event Subscriptions and complete URL
verification again.

## 9. Go-Live Gate

Run go-live only after `serve`, `up`, config, DB, and channels are ready:

```bash
argus doctor --deep --live --json
argus owner prove --team my-project --json
argus go-live --mode chat-only --public-url https://argus.example.com/slack
```

`go-live` does not replace the ownership proof. For each ownership-enabled team,
require both a successful `doctor --deep` dependency check and a successful
`owner prove` policy wiring check before calling the ownership loop live.

For quick tunnel smoke tests only:

```bash
argus go-live --mode chat-only --public-url https://example.trycloudflare.com/slack --dev-tunnel
```

Before first `go-live`, send one message in each configured Slack project
channel and confirm Argus replies. `go-live` checks recorded inbound and
outbound Slack activity per team channel. Add `--fresh-slack-proof` when you
want to force fresh proof from the last 30 minutes. To also prove every
configured Slack channel can post and clean up bot messages, run:

```bash
argus go-live --mode monitor-only \
  --prove-slack-channels \
  --require-source-type sentry \
  --require-source-type posthog \
  --require-each-team-source-type sentry \
  --require-each-team-source-type posthog \
  --public-url https://argus.example.com/slack
```

This posts one short check message per Slack channel and deletes it immediately.
The `--require-source-type` flags block `operational` status until at least one
source of each required type exists. The `--require-team-source-type` flags
block until one specific team has that source type. The
`--require-each-team-source-type` flags block until every team has that source
type. In monitor mode, configured sources must also dry-run cleanly.

The final status is one of:

- `operational`: DB, runtime, channel proof, engines, and mode checks passed.
- `configured-only`: config exists but an item was intentionally skipped or not
  configured.
- `blocked`: a required proof failed.

Mode-specific behavior:

- `chat-only`: requires DB migrated, `serve` reachable, `up` running, stable
  webhook URL, manager engine availability, Slack or fake-channel inbound
  proof, and outbound reply proof.
- `monitor-only`: also requires every configured connector dry-run to pass or
  be passed with `--skip-connector`.
- `pm-propose-pr`: requires developer, QA, senior, draft PR defaults, and a
  completed `open_pr` action from the last 24 hours. Use `argus pm run ...`
  for the smoke, then run `argus go-live --mode pm-propose-pr`. Pass
  `--skip-pr-smoke` only when you intentionally want `configured-only` instead
  of full operational proof.

## 10. Troubleshooting

- `@Argus` visible in Slack but no `events received`: Slack Request URL is wrong,
  tunnel is down, app lacks event subscription, or app is not in the channel.
- `events received` but no reply: `argus up --poll 10` is not running.
- Reply is `Got it.`: no non-echo `manager` role is configured for that team.
- Manager job fails: check `argus status`, `argus runs REQUEST_ID`, and engine
  CLI auth.
- PM opens no PR: check `test_cmd`, `github_repo`, `gh auth status`,
  `allow_network`, and branch permissions.
- launchd process starts but cannot read files: move runtime out of protected
  macOS folders or grant Full Disk Access.
- `go-live` says `configured-only`: inspect skipped or `not_configured` rows and
  decide whether they are acceptable for that mode.
