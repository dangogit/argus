# Showcase

These are the public-facing paths Argus is built to support. Each path should
end with `argus go-live`, not only generated config.

## Chat-Only Agent Manager

Use this when you want a Slack or Telegram agent manager that can answer status
questions and route work without touching code.

Typical setup:

```bash
argus onboard project /absolute/path/to/project --mode chat-only \
  --config /absolute/path/to/private/argus.yaml \
  --out-dir /absolute/path/to/private/onboarding \
  --channel slack --channel-id C1234567890

argus doctor --deep --json
argus go-live --mode chat-only --public-url https://argus.example.com/slack
```

Proof points:

- Slack or Telegram event received.
- Manager engine answers.
- Outbound reply sent.
- `up` worker runs continuously.

## Monitor-Only Operations Desk

Use this when you want production signals in one owner-controlled queue before
allowing code changes.

Supported source types include GitHub, Vercel, Firebase, Supabase, Sentry,
PostHog, Fly, uptime, Postgres, OpenAPI, webhooks, and support email paths.

Typical setup:

```bash
argus onboard project /absolute/path/to/project --mode monitor-only \
  --config /absolute/path/to/private/argus.yaml \
  --out-dir /absolute/path/to/private/onboarding \
  --channel slack --channel-id C1234567890

argus doctor --deep --require-source-type sentry --require-source-type posthog
argus poll --dry-run
argus go-live --mode monitor-only --public-url https://argus.example.com/slack \
  --require-source-type sentry --require-source-type posthog
```

Proof points:

- Required connector secrets are present.
- Connector dry-runs pass.
- Alerts are visible in Argus state.
- Missing optional connectors are explicitly skipped.

## PM Draft PR Loop

Use this when you want Argus to propose fixes through a safe pull request path.

Typical setup:

```bash
argus onboard project /absolute/path/to/project --mode pm-propose-pr \
  --config /absolute/path/to/private/argus.yaml \
  --out-dir /absolute/path/to/private/onboarding \
  --channel slack --channel-id C1234567890

argus doctor --deep
argus go-live --mode pm-propose-pr --public-url https://argus.example.com/slack
```

Proof points:

- Manager, developer, and QA role engines are configured.
- Worktree creation succeeds.
- Test command is viable.
- Risk policy blocks irreversible outward actions without approval.
- Smoke request produces a draft PR, or the operator explicitly passes
  `--skip-pr-smoke`.

## Agent-Assisted Installation

Codex, Claude Code, and other coding agents should read:

- [Agent Guide](../AGENTS.md)
- [Argus Live Onboarding Skill](../skills/argus-live-onboarding/SKILL.md)
- [Live Onboarding](live-onboarding.md)
- [Public Launch Checklist](public-launch.md)

The agent should inspect the host, ask only missing setup questions, write
private config, and prove the selected mode with `doctor --deep` and
`go-live`.
