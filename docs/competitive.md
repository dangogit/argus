# Competitive Landscape & Improvement Backlog

How Argus relates to other open-source agent projects, what it deliberately
does differently, and the prioritized backlog for closing real capability gaps.

Last reviewed: 2026-06-27.

## Who we compare against

| Project | One-line | Scope | Closeness |
|---|---|---|---|
| **Argus** | Self-hosted, propose-only company of agents for software ops | Team / project | - |
| [Paperclip](https://github.com/paperclipai/paperclip) | "The company" - agent org + kanban for work (TS, 71k★) | Team / project | **Direct peer** |
| [Hermes Agent](https://github.com/nousresearch/hermes-agent) | Personal agent that "grows with you" (204k★) | Single user | Different altitude |
| [OpenClaw](https://github.com/openclaw/openclaw) | Personal AI assistant, any OS, any channel (380k★) | Single user | Different altitude |
| [NanoClaw](https://github.com/nanocoai/nanoclaw) | "OpenClaw but small enough to understand" (~31k LoC, 30k★) | Single user | Different altitude |

Two camps:

- **Personal assistants** (OpenClaw, Hermes, NanoClaw) auto-execute on the host
  by default and serve one individual. Different altitude from Argus; surface
  overlap only (LLM agent + integrations).
- **Paperclip is the one true peer** - same "company of agents for work"
  framing, self-hosted, Postgres, multi-agent org, pluggable engines. The honest
  comparison lives below: Paperclip is **ahead** on orchestration UI, named
  agent org, per-agent budgets, and typed human interactions; Argus is **ahead**
  on production-signal monitoring, propose-only default, retro learning, and
  push-to-chat. Paperclip is execute-first with optional gates; Argus is
  propose-only by default. That trust line is the core distinction.

Note: Argus already integrates the Hermes Agent CLI (`hermes -z`) as one of its
execution engines (`src/argus/hermes/`). Paperclip also ships claude / codex /
hermes / openclaw adapters - the engine layer is a shared commodity, not a moat.

## Full feature matrix

Argus and Paperclip side by side (the peer comparison), with the
personal-assistant camp for reference.

| Axis | Argus | Paperclip (peer) | Hermes / OpenClaw / NanoClaw |
|---|---|---|---|
| Core identity | Dev-ops company, propose-only | Agent org + kanban for work | Personal assistants |
| Default trust | **Propose-only, 3-tier gates** | Execute-first, gates opt-in | Auto-execute |
| Watches production | **13 connectors** (sentry, vercel, posthog, fly, uptime, github, supabase, ...) | – (inbound = own UI + routines) | – |
| Retro / learning loop | **Daily, with injection quarantine** | – | Hermes self-models |
| Output channels | **Slack / WhatsApp / Telegram** | Web UI + webhooks | many (personal) |
| Eval / judge | **rubric scoring gates the PR** | promptfoo evals (offline) | – |
| Runtime | Postgres + advisory lock + NOTIFY | Embedded/external Postgres (Drizzle) | SQLite / gateway |
| Multi-agent | builder → QA → senior pipeline | **Named org chart, persistent identities** | subagents (Hermes) |
| Per-agent budgets | – (no global cap yet) | **monthly/lifetime, warn + hard-stop + incidents** | cost observability only |
| Human interactions | typed ask/confirm/suggest, idempotency-keyed (chat-native approve/reject; CLI nonce fallback) | **typed: ask/confirm/suggest, idempotency-keyed** | approval cards |
| Source-trust defense | retro quarantine only | **trust tier + quarantine + promotion on all content** | varies |
| Sandbox | git worktree | VM plugin sandbox; K8s/e2b/Daytona/Modal backends | Docker (`--internal` egress in NanoClaw) |
| Engines | echo/Codex/Claude/Hermes | claude/codex/cursor/gemini/grok/hermes/openclaw | own loop |
| MCP | none | ✅ (via hermes adapter + plugins) | Hermes client+server |
| Pipeline model | linear propose flow | **configurable Kanban, per-stage autonomy + gates** | n/a |
| Stars / age | pre-1.0 alpha | 71k★, ~4 mo | 30k-380k★ |

## What Argus does better

Measured honestly against the **peer (Paperclip)**, not just the
personal-assistant camp. Durable Postgres and engine-agnostic workers are *not*
on this list - Paperclip has both; they are table stakes, not a moat.

1. **Propose-only by default.** None of the five act this conservatively;
   Paperclip is execute-first with opt-in gates. For company production infra,
   propose-by-default is the right floor.
2. **Watches production.** 13 connectors turn Sentry, Vercel, PostHog, Fly, and
   uptime into routed, deduplicated work. Paperclip and the assistants have no
   production-signal monitoring - they wait for a human to file an issue. Argus
   reacts to the incident.
3. **Retro / learning loop with injection quarantine.** Daily retrospective that
   promotes lessons back into context, with candidates scanned for injection
   before they can auto-change anything. No competitor has an equivalent.
4. **Eval-judged draft-PR loop.** builder → QA → senior with an LLM judge that
   gates the diff (fail-closed) before a human sees it. Paperclip has pipeline
   approval but no inline eval gate on the work product.
5. **Pushes to chat.** Proposals land in Slack / WhatsApp / Telegram where the
   operator already is. Paperclip is web-UI + webhooks only.

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

## Patterns worth adopting (peer review)

Concrete mechanisms read out of competitor source. All repos are MIT - patterns
are adoptable; do not copy code verbatim. Several directly fix audit bugs below.

| Pattern | Source | What it is | Adopt for |
|---|---|---|---|
| **Per-agent / per-company budget** | Paperclip `services/budget*` | monthly + lifetime windows, warn %, hard-stop threshold, budget-incident records, pause-on-hit | Fixes HIGH "no global cost cap". Add a `budgets` table + `SUM(cost_usd)` gate in `sweep_once`. |
| **Typed human interaction + idempotency key** | Paperclip `issue-thread-interactions.ts` (uniq constraint) | `ask_user_questions` / `request_confirmation` / `suggest_tasks` are DB entities; an `idempotencyKey` unique constraint blocks duplicate prompts on retry | Fixes CRITICAL nonce design. Replace the bare 32-bit nonce with an idempotency-keyed approval row bound to `action_id`. |
| **Egress lockdown + credential proxy** | NanoClaw `egress-lockdown.ts` | agent container on a Docker `--internal` network; only exit is a gateway that injects creds, so the agent never holds raw keys; fails hard if gateway absent | Fixes HIGH `bash -lc` secret exposure. Run code-mode in a no-egress worktree container with a credential broker. |
| **Deterministic stuck detection** | NanoClaw `decideStuckAction()` (pure fn) + Paperclip `run-liveness.ts` (regex corpus) | classify planning-only / blocked / approval-required from stdout + evidence counts, no LLM call; pure function = unit-testable | Fixes blocked-job detection + observability gap. Add a pure `classify_run()` over Postgres job columns. |
| **Source-trust tier + quarantine + promotion** | Paperclip `source-trust.ts` | every doc/comment carries a trust preset; low-trust output substituted with a quarantine string in higher-trust context until a human promotes it | Argus injects GitHub/Slack text into agent context untrusted - tier inbound content, do not feed unreviewed external text into a system prompt. |
| **Capability-optional channel adapter** | OpenClaw `ChannelPlugin` | one interface, ~30 optional capability fields, manifest auto-discovery; 118 channels off one contract | The clean path to Discord / Signal / email (ROADMAP "Later"). Refactor connectors to this shape once. |
| **Routine catch-up cap** | Paperclip cron (`MAX_CATCH_UP_RUNS = 25`) | after downtime, cap backfilled scheduled runs + dedupe live runs before scheduling | Prevents a wake-up storm when Argus restarts after being down. |
| **Hook-module graceful degradation** | NanoClaw `router.ts` | core exposes named hooks; optional modules self-register via side-effect import; absent module = safe no-op default | Cleaner than scattered `if connector_enabled` checks for optional connectors. |
| **Deterministic context pre-pass** | Hermes `context_compressor.py` | collapse old tool results to 1-liners, dedup reads by hash, strip screenshots before any LLM summarize call | Free token savings on long worker runs. |

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

## Security & robustness hardening (internal audit)

These are implementation bugs found in a critical read of the v2 core, not
competitive gaps. The safety positioning makes them higher priority than most
features. Ranked by severity.

| Sev | Issue | Location | Fix direction |
|---|---|---|---|
| **CRITICAL** | Approval nonce is `token_hex(4)` = 32-bit bearer token that authorizes irreversible_outward actions; brute-forceable over the 24h TTL, and `ON CONFLICT (nonce) DO NOTHING` leaves a collided action stuck forever | `actions/executor.py:344`, `actions/approvals.py:15` | `token_hex(16)` (128-bit) + retry on collision; bind the consume check to `action_id` as well as nonce |
| HIGH | No logging in orchestrator / reconcile / worker / queue. If `sweep_once` raises, the orchestrator exits silently with no trace | `orchestrator/loop.py`, `worker/worker.py`, `queue/jobs.py` | Add module loggers; wrap `sweep_once` in try/except with backoff; structured log per claim/finalize/reclaim |
| HIGH | No global LLM cost cap. A signal storm enqueues unbounded paid jobs; `cost_usd` is stored in `runs` but never read for enforcement | `queue/jobs.py` | `SUM(cost_usd)` over 24h in `sweep_once`; halt enqueue over `company.defaults.max_daily_cost_usd` |
| HIGH | Retro feeds raw action payloads (reply text, email bodies, connector signals) into the learning LLM unsanitized; injection can ride into an auto-changeable candidate | `retro.py:463` | Run `sanitize.sanitize()` on payload text, or omit payloads from the retro packet (types/statuses suffice) |
| HIGH | `code-mode` runs `bash -lc <script>` - login shell loads profile and inherits env secrets; no ulimit/cgroup | `worker/exec.py:119` | Use `bash -c`; add `resource.setrlimit` in `preexec_fn`; pre-scan script for `curl|bash` patterns |
| MED | Advisory lock + LISTEN connection has no reconnect; on drop, a second orchestrator can start and NOTIFY wakeups silently degrade to 2s polling | `orchestrator/loop.py:18` | Reconnect loop that re-acquires lock and re-issues LISTEN; log fallback mode |
| MED | Connector driver swallows all exceptions with no log/backoff - an expired key retries every tick (silent provider retry storm) | `connectors/driver.py:89` | Log + record `error_count`/`last_error`; exponential backoff past a threshold |
| MED | `sanitize.py` strips injection phrases but not `postgres://` DSNs, emails, phone numbers - a DSN in a message can be distilled into the vault | `context/sanitize.py` | Add redaction patterns from `pm/scan.py` rules before distillation |
| MED | Retro `confidence`/`impact` thresholds for auto-change come straight from LLM JSON and are not range-clamped or evidence-verified | `retro.py:706` | Clamp to valid ranges; verify `evidence_run_ids` exist in `runs` |

**Subagent enforcement (adopt from Hermes):** when Argus spawns engine
subagents, intersect the child toolset with the parent and block `merge` /
`deploy` / `send` equivalents (`tools/delegate_tool.py` pattern). This enforces
propose-only structurally on delegated work, not just by prompt.
