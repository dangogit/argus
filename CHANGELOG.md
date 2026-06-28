# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Live progress in chat: when Argus picks up a message it posts an immediate
  receipt, and on edit-capable channels (Slack/Telegram/Discord) that receipt
  becomes a single self-updating status line that advances
  `👀 looking into this -> 🛠️ working -> 🔍 reviewing -> ✅ done` in place,
  instead of going silent or spamming a message per stage. Non-editable channels
  keep the one-shot receipt. Toggle with `notifications.show_progress` (default
  on).
- MCP client: configure external MCP servers in `argus.yaml` (`mcp.servers`);
  Argus validates them (`argus doctor --deep`) and feeds them to the Claude Code
  engine per run.
- MCP server: `argus mcp serve` exposes read-only `argus_status`, `argus_alerts`,
  `argus_lessons`, and `argus_proposals` tools over stdio JSON-RPC (local,
  single-operator trust boundary).
- Engines: `openrouter` and `ollama` (OpenAI-compatible HTTP), so engine choice
  is not tied to a vendor CLI.
- Channels: `discord` (poll inbound + send) and `email` (SMTP outbound; inbound
  via the existing `email_imap` connector).
- Budgets: `company.defaults.max_daily_cost_usd` and per-team
  `max_daily_cost_usd` pause new work when 24h spend reaches the cap; in-flight
  jobs still finish.
- Linux parity: `argus launchd render --os linux` emits systemd `.service`/
  `.timer` units for the full serve/up/poll/retro/watchdog/backup/logrotate
  bundle.

### Changed

- Orchestrator survives a crashing sweep (logs + backs off instead of exiting)
  and reconnects after a control-connection drop, handing off cleanly when
  another orchestrator holds the advisory lock.
- Run-liveness: a worker run that only planned, blocked, awaited approval, or hit
  a missing credential is classified and surfaced instead of looking done.
- Connectors back off on failure and record `error_count`/`last_error` instead
  of silently retrying a dead provider every tick.

### Fixed / Security

- Approval token widened to 128 bits with a collision-safe insert (no more
  brute-forceable token, no approval left permanently stuck).
- MCP-rendered config and Linux systemd unit files are written `0600`; systemd
  `Environment=`/`ExecStart` escape `%` so a DB password with `%` is not
  mangled; email send enforces STARTTLS before auth and rejects header
  injection.

## [0.2.0] - 2026-06-26

### Changed

- Argus is now a single Python v2 product. The `argus` console script points to
  `argus.v2.cli:main`, and the acceptance gate is `python scripts/gate.py`.
- Runtime state for alerts, triage, PM dispatches, memory, content, advisor,
  retro, and dashboard views is Postgres-backed.
- Operational commands now live in the Python CLI: setup, readiness,
  validation, PM, triage, support, content, context, advisor, WhatsApp,
  calendar, and host management.
- Host rendering supports launchd and systemd units for poll, queue worker,
  inbound, retro, watchdog, backup, and log rotation jobs.
- Documentation now describes one product surface and one local acceptance
  path.

### Added

- GitHub install script, Docker Compose pgvector smoke database, and release
  workflow for package build, `twine check`, and trusted PyPI publishing.
- Public docs homepage, FAQ, updating guide, showcase, launch checklist,
  support policy, issue templates, Dependabot config, and LLM-readable index.
- Guided project onboarding, deep doctor checks, and `go-live` proof for
  `chat-only`, `monitor-only`, and `pm-propose-pr` modes.
- Slack and Telegram public onboarding paths with secret validation and
  generic inbound handling.
- Python connectors for GitHub, Sentry, PostHog, Vercel, Firebase, Uptime,
  Supabase, Fly, and webhook sources.
- Google Calendar actions through the Python assistant action path.
- Advisor state migration to Postgres with importer support for existing local
  state.
- WhatsApp media handling, chunked replies, presence hooks, and optional local
  voice transcription.
- `argus config convert` for moving existing project manifests into the v2
  company config shape.

### Fixed

- Package metadata now uses SPDX license fields and includes the MIT license
  file without deprecated setuptools license classifiers.

### Removed

- The retired v1 command tree, shell modules, and Bats test suite.
- The separate v2 gate wrapper. `argus verify` and `python scripts/gate.py`
  now run the same acceptance gate.

## [0.1.0] - 2026-06-06

First public release. A self-hosted, engine-agnostic crew of autonomous agents
that watches projects, proposes fixes, and reports to the owner.

[Unreleased]: https://github.com/dangogit/argus/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dangogit/argus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dangogit/argus/releases/tag/v0.1.0
