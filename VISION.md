# Argus Vision

Argus is a self-hosted operations agent for software teams that want AI help
without handing production control to a hosted black box.

The product goal is simple: connect a project, prove what is live, then let
trusted operators ask for status, monitoring, and draft fixes from the channels
they already use.

## What Argus Should Be

- Private by default. Source repos, runtime config, secrets, and agent tools
  stay on infrastructure the operator controls.
- Operational, not theatrical. Setup is only complete when `argus go-live`
  proves the database, receiver, worker, channel, engine, connector, and PM
  path are working or explicitly skipped.
- Propose-only for code changes. Argus can open draft PRs, but it does not
  merge, deploy, or perform irreversible outward actions without approval.
- Agent-friendly. Codex, Claude Code, Hermes, and other coding agents should
  know how to install, configure, verify, and troubleshoot Argus from repo
  files alone.
- Boring under load. Postgres queues, role contracts, retries, approvals,
  dry-runs, and health checks matter more than clever demos.

## What Argus Is Not

- Not a hosted SaaS control plane.
- Not a credential vault.
- Not a replacement for CI, observability, incident response, or code review.
- Not a guarantee that every connector is configured just because YAML exists.

## Success Standard

Argus is successful when a new operator can:

1. Install it from GitHub or PyPI.
2. Connect Slack or Telegram.
3. Onboard a real repo.
4. Run `argus doctor --deep`.
5. Run `argus go-live`.
6. See a clear final state: `operational`, `configured-only`, or `blocked`.
7. Ask for status in chat and get a useful, evidence-backed answer.
8. Enable monitor-only or PM draft PR mode without weakening safety defaults.

Anything less is setup progress, not operational readiness.
