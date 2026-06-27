# FAQ

## What is Argus?

Argus is a self-hosted company of AI agents for software projects. It watches
project channels, repos, production signals, support inboxes, and scheduled
sources, then routes work through approval-gated agents.

## Is Argus a chatbot?

No. Chat is one control surface. The core product is a Postgres-backed runtime
with queues, role pipelines, approvals, connector dry-runs, always-on workers,
and operational proof gates.

## What should I install first?

Use the installer from the repository root:

```bash
curl -fsSL https://raw.githubusercontent.com/dangogit/argus/main/scripts/install.sh | sh
argus --version
```

Then run the local smoke in [Quickstart](quickstart.md). Keep `echo` as the
first engine so runtime problems are separated from model or credential
problems.

## Can Codex or Claude Code install it for me?

Yes. Point the coding agent at
[AGENTS.md](../AGENTS.md) and
[skills/argus-live-onboarding/SKILL.md](../skills/argus-live-onboarding/SKILL.md).
The skill tells the agent to inspect the computer first, ask only missing setup
questions, write private config, and prove `doctor --deep` plus `go-live`.

## Which channels work today?

Argus supports CLI, fake, Slack, Telegram, and WhatsApp channel adapters. Slack
and Telegram are the best public onboarding paths because they have documented
webhook setup and secret validation.

## Is Slack app creation automatic?

No. Each user creates their own Slack app or reuses an existing one. Argus
provides [a manifest template](../examples/slack-app-manifest.yaml) and
[Slack setup docs](slack-live.md). The app credentials stay in private env.

## Does Argus read my `.env` secrets?

`argus onboard project` detects env key names, but it must not copy secret
values. Runtime secrets belong in private env files loaded with
`ARGUS_ENV_FILES`, or in the operator shell environment.

## What does "operational" mean?

`argus validate` proves config shape. `argus doctor --deep` proves engines,
repos, channels, and connector dry-runs. `argus go-live` proves DB migrations,
receiver reachability, continuous worker processing, channel event receipt,
outbound reply, and a draft PR smoke when `pm-propose-pr` mode is enabled.

If `go-live` says `configured-only` or `blocked`, the install is not
operational yet.

## Can Argus change my code?

In `pm-propose-pr` mode, Argus uses an isolated worktree and opens draft pull
requests by default. It does not merge into your base branch. Code-changing
autonomy is an explicit per-project opt-in.

## Which engines are supported?

Use `echo` first. For real work, Argus can call Codex, Claude Code, or Hermes
when those CLIs are installed and authenticated. `doctor --deep` checks every
configured role engine.

## What providers can Argus monitor?

Argus has connector coverage for GitHub, Vercel, Firebase, Supabase, Sentry,
PostHog, Fly, uptime, Postgres, OpenAPI, webhooks, and support email paths.
Connectors are not considered live until dry-runs pass or they are explicitly
skipped.

## Does Argus need a hosted account?

No. Argus is self-hosted. External accounts are only needed for channels,
models, GitHub PRs, or provider APIs you choose to connect.

## Can I run it on a laptop?

Yes for development and owner use. For real webhooks, use a stable public URL
and a process manager. Quick tunnels are smoke tests only unless you accept
their downtime.

## How do I update or remove Argus?

See [Updating](updating.md).
