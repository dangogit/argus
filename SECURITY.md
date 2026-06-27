# Security Policy

Argus runs with real access to a machine you own and can open pull requests on
your repositories, so its security model matters. This document explains the
intended boundaries and how to report a problem.

## Reporting a vulnerability

Please report security issues privately, not in public issues.

- Preferred: open a private advisory via the repository's **Security** tab ->
  **Report a vulnerability** (GitHub Security Advisories).

Include what you did, what happened, and why it is a problem. We aim to
acknowledge within a few days. Please give us a reasonable window to fix before
public disclosure.

## Supported versions

Argus is pre-1.0. Security fixes target the latest `main` and the most recent
`0.1.x` release.

## Security model (intended boundaries)

See [docs/threat-model.md](docs/threat-model.md) for the surface-by-surface control map.

Understanding these helps you tell a bug from intended behavior.

- **Self-hosted, host-trusted.** Argus runs on a machine you control and uses
  your installed agent CLI (Claude Code or Codex) with that engine's own
  permission model. It does not add a separate sandbox around the engine. Treat
  the host and whoever can run the `argus` CLI as trusted.
- **Propose-only by default.** The auto-fix pipeline runs in an isolated git
  worktree and writes a patch; it never pushes to a base branch and never
  merges. `propose-pr` opens a **draft** PR for review; `autonomous` is an
  explicit, per-project opt-in. No change reaches your default branch without a
  human.
- **Inbound command channel is the trust boundary.** The optional WhatsApp
  router fails closed at independent gates: transport token, `fromMe` drop,
  owner allowlist, and a closed verb set. The only mutating command, `approve`,
  can only flip a draft PR that Argus itself recorded to ready-for-review.
  Untrusted input must never reach a tool-capable agent.
- **Secrets stay out of the repo.** Tokens and keys live in your private overlay
  (`<overlay>/.env`) or the environment, never in the tracked tree. The `run/`
  directory (alerts, findings, patches, state) is gitignored.

## What we especially want to hear about

- Any path where untrusted input (a webhook body, a group message, a triage
  finding) can run code, read secrets, or reach a tool-capable agent.
- Any way to make the pipeline push or merge to a base branch without the
  documented opt-in.
- Any way to drive the inbound command channel beyond its four gates or its
  closed verb set.
- Secret leakage in logs, digests, alerts, or generated patches.
