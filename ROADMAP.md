# Roadmap

Argus is a self-hosted company of AI agents for software projects. The priority
order is what makes operators adopt and keep the system: **more capability** (the
agents help with more) and **reliability** (the agents do not silently break).
Pure-security hardening is tracked and will land, but is sequenced after - except
where a fix is also a reliability fix, in which case it rides in the reliability
lane.

## Shipped

Landed June 2026 (#7). The reliability foundation plus the first capability wave;
see [CHANGELOG.md](CHANGELOG.md) and [docs/competitive.md](docs/competitive.md).

- **Reliability:** runtime observability + crash backoff (a crashed sweep no
  longer exits silently); deterministic run-liveness (planning-only / blocked /
  approval-required / missing-creds surfaced); connector failure backoff + error
  visibility; 128-bit collision-safe approvals (no brute-force, no stuck
  approval); global + per-team daily cost ceilings; orchestrator reconnect after
  a control-connection drop with clean leadership handoff.
- **Capabilities:** MCP client (configure servers, validated by `argus doctor`,
  fed to the Claude Code engine); MCP server (`argus mcp serve`, read-only
  status/alerts/lessons/proposals); OpenRouter + Ollama engines; Discord + email
  gateway channels; Linux systemd parity for the always-on bundle
  (`argus launchd render --os linux`).
- **Security hardening:** secret-bearing MCP/systemd files written `0600`;
  systemd `%` escaping; SMTP STARTTLS-before-auth + header-injection guard; MCP
  malformed-frame guard.

## Now

- Keep public install green: source install, wheel smoke, Docker smoke, and
  public launch checker; improve live onboarding and Codex/Claude Code docs.
- Triage the default-branch dependency vulnerabilities (Dependabot).

## Next

- Richer human interactions: typed ask / confirm / suggest, not only approve /
  reject (the per-team budget half of this item shipped).
- MCP P1: a live protocol handshake in `argus doctor` (beyond config validation)
  and caps on untrusted MCP tool output.
- MCP P2: gated action tools (propose / approve) over MCP, routed through the
  existing approval gate.

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
