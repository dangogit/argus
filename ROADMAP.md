# Roadmap

Argus is a self-hosted company of AI agents for software projects. The priority
order is what makes operators adopt and keep the system: **more capability** (the
agents help with more) and **reliability** (the agents do not silently break).
Pure-security hardening is tracked and will land, but is sequenced after - except
where a fix is also a reliability fix, in which case it rides in the reliability
lane below.

## Now - reliability foundation (adoption blockers)

These are the "it just works, and when it doesn't I can see why" basics. Without
them, capabilities built on top inherit the same silent failures.

- Runtime observability: structured logging across orchestrator, worker, and
  queue, plus crash backoff so a stalled pipeline is visible, not silent. Today
  a crashed sweep exits with no trace - operators cannot debug it.
- Deterministic run-liveness detection: classify planning-only, blocked, and
  approval-required runs from evidence, without a second model call, so stuck
  jobs surface instead of hanging.
- Connector hardening: backoff on failure, clear missing-secret / expired-key
  states, and dry-run output - so a dead provider degrades loudly, not as a
  silent retry storm.
- Approval reliability: an approval can currently collide and get stuck forever
  (a click that never opens the PR). Make approvals idempotency-keyed and bound
  to their action id, with retry on collision. (Also closes the weak-nonce
  security gap as a side effect.)
- Cost ceiling: a global daily spend cap that pauses new work, so a signal storm
  cannot run away with the bill and erode trust.
- Orchestrator resilience: re-acquire the advisory lock and re-`LISTEN` after a
  database connection drop, instead of silently degrading to slow polling.
- Keep public install green: source install, wheel smoke, Docker smoke, and
  public launch checker; improve live onboarding and Codex/Claude Code docs.

## Next - capabilities (the agents help with more)

- MCP client support so Argus agents use the whole MCP tool ecosystem without a
  new connector each time. See [docs/specs/mcp-support.md](docs/specs/mcp-support.md).
- MCP server (`argus mcp serve`) exposing status, alerts, proposed PRs, and
  lessons, so Claude Code / Codex / IDEs can drive Argus.
- Provider breadth: OpenRouter and Ollama paths so users are not locked to one
  agent CLI.
- More ops channels behind one capability-optional connector interface, starting
  with Discord and a generic email gateway.
- Richer multi-agent work and human interactions: typed ask / confirm / suggest
  (not only approve / reject), and per-agent budgets with warn + hard-stop.
- Linux runtime parity for the opinionated always-on bundle.

## Later - hardening and polish

- Code-mode execution sandbox: run generated code in a no-egress worktree
  container with a credential broker, so the agent never holds raw secrets.
- Source-trust tiering: quarantine untrusted inbound content (GitHub, chat)
  until a human promotes it, before it enters an agent's context.
- agentskills.io compatibility for portable skills.
- Better public examples for monitor-only and pm-propose-pr modes.
- Hosted docs site if README and docs directory become hard to navigate.

## Non-goals

- No hosted Argus cloud holding user secrets.
- No auto-merge or auto-deploy by default.
- No general personal assistant scope.
- No smart home, voice assistant, or social media automation surface.
