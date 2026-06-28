# Spec: MCP Support (client + server)

Status: Draft
Owner: TBD
Roadmap: Next ([ROADMAP.md](../../ROADMAP.md))
Related: [docs/competitive.md](../competitive.md), GitHub issue #2 "[roadmap] Add MCP client support"
Last updated: 2026-06-28

## Problem Statement

Argus has no Model Context Protocol (MCP) support of its own. Today every
integration is hand-written: a Python `Connector` for each signal source and a
per-engine adapter for each agent CLI. The only MCP touch point is a
`--strict-mcp-config` passthrough flag handed to the Claude Code engine
(`src/argus/engine/adapters/claude_code.py:32`). MCP is now the de-facto
integration standard - the two closest projects (Hermes is an MCP client and
server; Paperclip exposes MCP through its adapters and plugin layer) both speak
it, while Argus does not. The cost of not solving this: every new tool or data
source is bespoke maintenance, Argus cannot be driven from MCP-aware clients
(Claude Code, Codex, Cursor, IDEs), and Argus's agents cannot reuse the growing
ecosystem of MCP servers.

## Goals

1. **Argus agents can use external MCP tools.** A worker run can call tools from
   operator-configured MCP servers without new Python, measured by external MCP
   servers being callable in a real run.
2. **Argus is drivable over MCP.** An MCP-aware client can read Argus status,
   alerts, proposed PRs, and retro lessons, and request gated actions, measured
   by a reference client (Claude Code) completing those calls.
3. **Propose-only is preserved across the MCP boundary.** No MCP path - inbound
   or outbound - can execute an irreversible action without the existing risk
   classification and approval gate. Zero gate bypasses in the threat review.
4. **Displace bespoke integration code.** New integrations land as MCP server
   config rather than new `Connector` subclasses, measured by integrations added
   via MCP vs hand-written connectors over the first quarter.
5. **Stay echo-safe.** MCP client and server both work in the no-live-engine
   smoke path so `argus doctor` and CI can validate them without model keys.

## Non-Goals

1. **Not an MCP server marketplace / registry.** Argus consumes and exposes MCP;
   it does not host a discovery registry. Out of scope - separate initiative,
   the ecosystem (agentskills.io, public registries) already covers it.
2. **Not autonomous MCP server installation by the agent.** The agent cannot add
   or enable an MCP server on its own; operators configure servers in
   `argus.yaml`. Out of scope - that is a self-modification surface and conflicts
   with the trusted-operator boundary.
3. **Not a replacement for existing connectors in v1.** The 13 native connectors
   stay; MCP is additive. Rewriting them onto MCP is a later, separate decision.
4. **Not full bidirectional streaming / sampling.** MCP "sampling" (server asks
   the client to run a model) is out of scope for v1 - it inverts control and
   needs its own safety review.
5. **Not multi-tenant MCP auth.** The MCP server trusts the same operator
   boundary as the rest of Argus (one token / local socket), not per-user scopes.

## User Stories

### Operator (runs Argus)
- As an operator, I want to register an external MCP server in `argus.yaml` so
  that my Argus agents can use its tools without me writing a connector.
- As an operator, I want `argus doctor` to verify each configured MCP server is
  reachable and its tools enumerable so that I find misconfiguration before
  go-live, not during an incident.
- As an operator, I want MCP tool output treated as untrusted input so that a
  compromised or malicious MCP server cannot inject instructions into my agents.

### Agent worker (Argus internal)
- As an Argus worker run, I want the tools from allowed MCP servers presented to
  my engine so that I can call them during a PM or support task.
- As an Argus worker run, I want oversized MCP tool results stored and previewed
  rather than inlined so that one large response cannot poison my context.

### MCP client (Claude Code / Codex / IDE)
- As a Claude Code user, I want to ask Argus "what is broken right now?" over MCP
  so that I can triage from my editor without opening the dashboard.
- As an MCP client, I want to request "propose a fix for alert X" so that Argus
  opens a draft PR through its normal gated pipeline, not an auto-merge.
- As an MCP client, I want to read retro lessons for a project so that my own
  agent avoids repeating known mistakes.

### Security reviewer
- As a security reviewer, I want every MCP-exposed action that mutates state or
  reaches outward to pass the same `risk_for()` classification and approval gate
  as a chat-originated action, so that MCP is not a privilege-escalation path.

## Requirements

### Must-Have (P0) - MCP client (Argus consumes)

**P0-1. Configure external MCP servers.**
`argus.yaml` accepts an `mcp.servers` block: name, transport (`stdio` command or
`http`/`sse` url), env/secret refs (by name, never inline values, consistent
with existing secret handling), and an optional per-server tool allowlist.
- Given an `mcp.servers` entry with a valid stdio command
- When Argus loads config
- Then the server is registered and its tools are enumerable
- [ ] Schema validated by `argus validate`
- [ ] Secret values referenced by env name, never stored in YAML
- [ ] Unknown/invalid transport fails validation with a clear message

**P0-2. Expose MCP tools to engine workers.**
Worker runs receive the allowed MCP tools through the engine adapter that
supports them (Claude Code first, via its native MCP config; others as adapters
gain support). Engines without MCP support degrade gracefully (tools omitted, no
crash).
- Given a configured MCP server and a Claude Code worker run
- When the run executes
- Then the server's allowed tools are available to the model
- [ ] Engine without MCP support omits tools and logs a one-line notice
- [ ] Per-role least-privilege still applies (read-only roles get read-only MCP tools)

**P0-3. Treat MCP tool output as untrusted + size-capped.**
MCP tool results are size-capped per result and per turn; oversized results are
persisted and replaced with a short preview. Results are tagged as
external/untrusted provenance (interim: fenced + not promoted to durable context
until source-trust tiering lands; see Open Questions).
- Given an MCP tool returns a 2 MB payload
- When the worker ingests it
- Then the inline context gets a capped preview and a stored-artifact reference
- [ ] Per-result and per-turn caps configurable with safe defaults
- [ ] MCP output never written to the context vault as a durable fact in v1

**P0-4. `argus doctor` MCP check.**
`argus doctor --deep` dry-runs each configured MCP server: connect, list tools,
report unreachable/auth-failed servers as findings (fail-closed - a required MCP
server that is down blocks `operational`, mirroring connector `--require-*`).
- [ ] Reachable server → info finding with tool count
- [ ] Unreachable/auth-failed → error finding
- [ ] `--require-mcp <name>` makes a missing server block go-live

**P0-5. Echo-safe.**
Client config, validation, and doctor checks run without a live engine.
- [ ] `argus validate` + `argus doctor` MCP checks pass on the echo smoke path

### Nice-to-Have (P1) - MCP server, read-only (Argus exposed)

**P1-1. `argus mcp serve` read surface.**
A local MCP server exposing read-only Argus primitives as MCP tools/resources:
`status`, `alerts` (list/get), `proposals` (open draft PRs + pending approvals),
`lessons` (retro knowledge by project/team).
- Given `argus mcp serve` is running and a client connects
- When the client calls `alerts.list`
- Then it receives current open alerts as structured data
- [ ] Auth required (token; see Open Questions) - no unauthenticated reads
- [ ] Read tools never mutate state

**P1-2. Reference client proof.**
Documented Claude Code config that connects to `argus mcp serve` and completes
`status` + `alerts.list` + `lessons.get`.
- [ ] `docs/` walkthrough with a copy-paste client config

### Future Considerations (P2)

**P2-1. MCP server gated actions.**
Expose `propose_fix`, `submit`, and `approve` as MCP tools that route through
`risk_for()` + the approval gate. `approve` over MCP must use the hardened,
idempotency-keyed approval (depends on the Approval-Hardening item) - never a
raw nonce. Design the read surface (P1) so these slot in without rework.

**P2-2. MCP-as-connector.**
A `mcp` connector `type` (`src/argus/v2/connectors/base.py` REGISTRY) that polls
an MCP server's resources and emits `Signal`s, so an MCP server can be a signal
source, not just a tool surface.

**P2-3. Additional engines' MCP tool support** (Codex, Hermes) as their CLIs
expose MCP configuration.

## Success Metrics

### Leading indicators (days–weeks)
- **Client adoption**: # installs with ≥1 configured MCP server within 30 days of
  release. Target: 30% of active installs. Stretch: 50%.
- **Tool usage**: median MCP tool calls per PM/support run where a server is
  configured. Target: ≥1.
- **Doctor pass rate**: % of configured MCP servers passing `doctor --deep`.
  Target: ≥95% (the rest are real misconfigurations doctor surfaced).
- **Server reads**: # distinct MCP clients connecting to `argus mcp serve` per
  install (P1). Target: ≥1 within 30 days.

### Lagging indicators (weeks–months)
- **Integration displacement**: ratio of new integrations added via MCP config
  vs new hand-written `Connector` subclasses over a quarter. Target: ≥2:1.
- **Connector maintenance load**: connector-related issues/PRs per quarter trend
  down after MCP-as-connector (P2-2) ships.
- **Ecosystem pull**: Argus referenced as MCP-compatible in competitive docs and
  community; reduces the "no MCP" gap called out in `docs/competitive.md`.

Measurement: anonymous, opt-in runtime counters only (consistent with the
no-telemetry-without-opt-in posture); otherwise self-reported via Discussions.

## Open Questions

- **[engineering] Transport for v1?** stdio-only (simplest, local) vs also
  HTTP/SSE for client; stdio vs HTTP for the server. Blocking for P0-1/P1-1.
- **[security] MCP server auth.** Reuse `ARGUS_DASHBOARD_TOKEN`, a dedicated
  `ARGUS_MCP_TOKEN`, or local-socket-only? Blocking for P1-1.
- **[security/eng] Untrusted MCP output handling.** P0-3 caps + fences as an
  interim. Full safety depends on source-trust tiering (roadmap Later). Decide
  whether P0 ships with the interim or waits. Non-blocking if interim accepted.
- **[engineering] Per-engine MCP capability matrix.** Claude Code supports MCP
  config natively; confirm Codex/Hermes paths and whether `echo` should stub an
  MCP tool for tests. Blocking for P0-2.
- **[product] First server primitives.** Confirm `status / alerts / proposals /
  lessons` is the right read set for P1, and the action set for P2.
- **[data] Cost attribution.** Are MCP tool calls priced/attributed in the
  `runs.cost_usd` provenance, or out of scope for cost tracking? Non-blocking.

## Timeline Considerations

- **Sequencing.** P2-1 (server gated actions) depends on the **Approval
  Hardening** item (roadmap Now) - land that first so MCP `approve` uses the
  idempotency-keyed approval, not the legacy nonce. P0 (client) has no such
  dependency and can start immediately.
- **Phasing.**
  - Phase 1 (P0): MCP client - config, engine tool exposure, untrusted-output
    caps, doctor check, echo-safe. Ships the core value (use the ecosystem).
  - Phase 2 (P1): read-only `argus mcp serve` + reference client.
  - Phase 3 (P2): gated action tools (after Approval Hardening) + MCP-as-connector.
- **No hard external deadline.** Driven by closing the competitive gap, not a date.
- **Dependency to watch.** Source-trust tiering (roadmap Later) upgrades P0-3
  from interim caps to full quarantine; design P0-3 so that swap is additive.
