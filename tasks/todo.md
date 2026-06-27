# Roadmap build loop - progress tracker

Goal: build every Now (reliability) + Next (capabilities) roadmap item end to
end (plan -> execute -> test) until Argus is genuinely top tier. Driven by the
`/loop` self-paced loop. Each iteration: pick the next unchecked item, plan it,
implement with a test, verify, check it off.

Branch: `feat/reliability-capabilities`. Commit per completed+tested item.

## Now - reliability foundation
- [x] 1. Runtime observability: module loggers across orchestrator/worker/queue; wrap sweep in try/except + crash backoff; structured log per claim/finalize/reclaim
- [ ] 2. Deterministic run-liveness detection: pure classifier (planning-only / blocked / approval-required) over job evidence, no second model call
- [ ] 3. Connector hardening: failure backoff, missing-secret / expired-key states, dry-run output
- [ ] 4. Approval reliability: idempotency-keyed approval bound to action_id, 128-bit token, retry on collision (also closes weak-nonce security gap)
- [ ] 5. Cost ceiling: global daily spend cap that pauses new work
- [ ] 6. Orchestrator resilience: re-acquire advisory lock + re-LISTEN after DB connection drop

## Next - capabilities
- [ ] 7. MCP client P0 (per docs/specs/mcp-support.md): config, engine tool exposure, untrusted+capped output, doctor check, echo-safe
- [ ] 8. MCP server read-only: argus mcp serve exposing status/alerts/proposals/lessons
- [ ] 9. Provider breadth: OpenRouter + Ollama engine paths
- [ ] 10. More channels: Discord + generic email gateway behind a capability-optional interface
- [ ] 11. Richer multi-agent / typed interactions: ask/confirm/suggest + per-agent budgets
- [ ] 12. Linux runtime parity: opinionated always-on bundle as systemd units

## Done log
- Item 1 (observability): loop.py resilient `_sweep` + `_backoff_seconds` (sweep crash logs + backs off, no silent orchestrator death); loggers in orchestrator/queue/worker/reconcile; structured logs on claim/finalize/reclaim + worker failure. Test: tests/python/v2/test_orchestrator_loop.py (5 passed). Regression: queue/fencing/reconcile/worker (26 passed).
