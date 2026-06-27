# Competitive Landscape & Improvement Backlog

How Argus relates to other open-source agent projects, what it deliberately
does differently, and the prioritized backlog for closing real capability gaps.

Last reviewed: 2026-06-27.

## Who we compare against

| Project | One-line | Scope |
|---|---|---|
| **Argus** | Self-hosted, propose-only company of agents for software ops | Team / project |
| [OpenClaw](https://github.com/openclaw/openclaw) | Personal AI assistant, any OS, any channel | Single user |
| [Hermes Agent](https://github.com/nousresearch/hermes-agent) | Personal agent that "grows with you" | Single user |

Both competitors are general-purpose **personal assistants that auto-execute on
the host by default**. Argus occupies a different point: a **propose-only
operations layer** with software-project domain awareness. Surface overlap (LLM
agents + integrations) is real; the use case and trust model are not the same.

Note: Argus already integrates the Hermes Agent CLI (`hermes -z`) as one of its
four execution engines (`src/argus/hermes/`, `src/argus/engine/adapters/`). The
relationship is partly complementary, not purely competitive.

## Full feature matrix

| Axis | Argus | OpenClaw | Hermes Agent |
|---|---|---|---|
| Core identity | Dev-ops company layer | Personal assistant | Personal agent |
| Default trust | **Propose-only, 3-tier approval gates** | Auto-execute (full host) | Auto-execute + targeted gates |
| Domain awareness | repo / PR / prod / support / incident | none | none |
| Signal connectors | **13** (github, branch_drift, sentry, vercel, posthog, supabase, fly, firebase, uptime, postgres, openapi, webhook, email_imap) | n/a | n/a |
| Inbound channels | 3 (Slack, WhatsApp, Telegram) | 22+ | 8 |
| Execution engines | echo, Codex, Claude Code, Hermes | own loop | own loop |
| Model providers | via engine CLI | "all major" (WIP) | 200+ (OpenRouter, Ollama, ...) |
| MCP | none | unclear | client + server |
| Skills | local, team-curated | ClawHub registry | autonomous self-improving + agentskills.io |
| Sandbox | git worktree isolation | Docker / SSH | 6 backends |
| Memory / learning | retro + context vault (curated) | SOUL/AGENTS.md | Honcho user-model + FTS5 |
| Runtime | **Postgres queues + advisory lock + LISTEN/NOTIFY** | WS gateway daemon | gateway + TUI |
| Role pipeline | **builder → QA → senior** | – | delegate subagents |
| Eval / judge | **rubric scoring on diffs** | – | – |
| Draft-PR loop | **worktree + QA + secret scan + caps** | – | – |
| Deploy targets | macOS launchd + Linux systemd (`host render`) | launchd + systemd | broad-OS 1-liner |
| Voice | WhatsApp voice-in (whisper) | wake word + TTS/STT | Whisper + ElevenLabs |
| Mobile apps | – | iOS / Android nodes | – |

## What Argus does better

1. **Propose-only by default.** The only one of the three that will not act on
   production without an explicit human approval. The right trust model for
   company infrastructure.
2. **Software-ops domain.** 13 connectors turn repos, Sentry, Vercel, PostHog,
   Fly, and uptime into routed, deduplicated work. The competitors have zero
   PR / incident / support-ticket awareness.
3. **Durable Postgres runtime.** Queues, single-orchestrator advisory lock,
   wake-on-`NOTIFY`. Survives restarts; it is not a chat loop.
4. **Role pipeline + eval gate.** builder → QA → senior with an LLM judge
   scoring the diff before a human ever sees the PR.
5. **Engine-agnostic.** Runs Claude Code, Codex, or Hermes as interchangeable
   workers, with least-privilege toolsets per role.

## Engineering substance behind "propose-only"

These are implemented mechanisms (not positioning) that back the safety claim and
that the competitors largely lack. Advertise them.

- **Server-side risk override** (`worker/worker.py:137`, `actions/executor.py:46`).
  The model's declared `risk` is discarded and recomputed server-side before the
  gate. Converse jobs force the PR repo + number server-side, so a prompt-injected
  model cannot redirect a PR to another repo.
- **Fail-closed eval gate** (`evals/judge.py:48`). Judge error, garbled JSON, or a
  missing marker all yield score 0.0, which holds the draft. Never fails open.
- **Fencing-token CAS on finalize** (`queue/jobs.py:106`). A reclaimed job rotates
  its claim token, so a stale/zombie worker is provably locked out. Claim uses
  `FOR UPDATE SKIP LOCKED` - no thundering herd, no app-level mutex.
- **Diff secret-scan before a PR opens** (`pm/scan.py`). Scans added lines for
  PEM keys, AKIA tokens, `sk-` keys, `password=`. CRITICAL findings block the PR.
- **Retro injection quarantine** (`retro.py:361`). Learned candidates are scanned
  for injection phrases before they can bridge to knowledge or auto-change.
- **SAVEPOINT-per-action drain** (`actions/executor.py:209`), **atomic vault writes**
  (`context/vault.py:139`), **per-source connector transaction isolation**
  (`connectors/driver.py:75`).

## What we deliberately do NOT do (non-goals)

These are design choices, not missing features. Do not "fix" them without a
deliberate scope change.

- **Not a personal assistant / general host executor.** Argus serves a project,
  not an individual's daily life.
- **No auto-execute, no auto-merge, no auto-deploy** by default. Outward and
  irreversible actions are approval-gated.
- **Not a hosted SaaS.** You own the database and the secrets; there is no
  Argus cloud holding your keys.
- **No mobile apps, voice assistant, smart home, or social media** surfaces.
- **Skills are curated, not autonomously self-mutating.** Conservative on
  purpose for an unattended ops agent.

## Improvement backlog

[`ROADMAP.md`](../ROADMAP.md) is the canonical short list (Now / Next / Later).
This section is the detailed design rationale behind those items, plus the
internal-audit hardening work that informs them.

Prioritized for the next cycle. P0 = highest leverage.

### P0 - MCP support

**Gap:** Argus has no Model Context Protocol support of its own. The only MCP
reference today is the `--strict-mcp-config` passthrough flag to the Claude Code
engine (`src/argus/engine/adapters/claude_code.py:32`). Hermes is both an MCP
client and server; MCP is becoming the default integration standard.

**Proposal:**
- **MCP client** so Argus connectors/tools can call external MCP servers,
  giving every engine a uniform tool surface instead of bespoke connector code.
- **MCP server** (`argus mcp serve`) exposing Argus's own primitives (status,
  alerts, propose-PR, retro lessons) so other agents and IDEs can drive Argus.
- Start client-side; reuse the existing `connectors/driver.py` + `skills/`
  registry rather than a parallel system.

**Why first:** biggest differentiation lever vs Hermes, and it multiplies the
connector story instead of adding one-off integrations.

### P1 - Model-provider breadth

**Gap:** the engine layer is CLI-driven (echo / Codex / Claude Code / Hermes).
Provider selection only exists inside the Hermes profile (`hermes/profile.py`,
default `anthropic`). No OpenRouter / Ollama / direct-API path, so users are
locked to whichever agent CLI they installed.

**Proposal:**
- A thin provider abstraction for engines that can take a `provider/model` id
  (mirror Hermes's `agent.model: "<provider>/<model-id>"`).
- First targets: **OpenRouter** (breadth) and **Ollama** (local / offline).
- Keep it opt-in; `echo` stays the default safe engine.

### P2 - Adopt the agentskills.io standard

**Gap:** Argus skills are a local registry. Hermes consumes the open
`agentskills.io` standard, and Argus's own Hermes engine already points
`skills_dir` at `~/agent-skills` (`hermes/profile.py:90`). Adopting the standard
is low-friction and makes the ecosystems interoperable.

### P3 - Channel breadth

**Gap:** 3 inbound channels (Slack / WhatsApp / Telegram) vs 8-22. For an ops
tool, **Discord**, **Signal**, and a **generic email gateway** are the
highest-value additions. Mobile apps and the long tail (WeChat, LINE, Matrix,
...) are explicitly out of scope.

**Pattern to adopt (OpenClaw, clean MIT):** before adding channels one-off,
define a capability-optional channel/connector interface like OpenClaw's
`ChannelPlugin` (`src/channels/plugins/types.plugin.ts`): one contract with ~30
*optional* fields grouped by capability (inbound, outbound, pairing, allowlist,
doctor, threading, streaming), auto-discovered from a manifest. OpenClaw runs
118 channels off that one interface; a channel only implements the fields it
needs. Argus connectors currently hardcode their integration points - refactor
to this shape once, then Discord / Signal / email are thin adapters.

Also worth adopting from OpenClaw: a file-based per-channel pairing-code flow +
allowlist (vs today's `OWNER_ALLOWLIST` env var), and a security-posture check
in `argus doctor` that flags dangerous config combos (open group + outbound
actions enabled), not just connectivity.

### P4 - Linux runtime parity (mostly done - verify, don't rebuild)

**Status:** Linux deploy is **largely already supported**. `host.py` renders
systemd service + timer units and auto-detects the OS
(`render_systemd_service`, `render_systemd_timer`, `default_os` →
`linux`), driven by `argus host render --os linux`.

**Remaining gap:** the opinionated 7-job runtime bundle (serve, up, poll, retro,
watchdog, backup, logrotate) is hardcoded in `launchd.py` for macOS only. The
generic `host render` path takes a `--jobs-dir` instead. The fix is to render
the same curated bundle as systemd units out of the box, plus docs - not a new
deploy system.

### Smaller hardening items

- Declare external binary deps (`whisper-cli`, `hermes`) so engines fail loud,
  not silently unavailable.
- Connector error back-off (today polling is a fixed `StartInterval`).
- The single-orchestrator advisory lock blocks horizontal scale; fine for now,
  note it as a known ceiling.
