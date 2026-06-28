# Roadmap build loop - progress tracker

Goal: build every Now (reliability) + Next (capabilities) roadmap item end to
end (plan -> execute -> test) until Argus is genuinely top tier. Driven by the
`/loop` self-paced loop. Each iteration: pick the next unchecked item, plan it,
implement with a test, verify, check it off.

Branch: `feat/reliability-capabilities`. Commit per completed+tested item.

## Now - reliability foundation
- [x] 1. Runtime observability: module loggers across orchestrator/worker/queue; wrap sweep in try/except + crash backoff; structured log per claim/finalize/reclaim
- [x] 2. Deterministic run-liveness detection: pure classifier (planning-only / blocked / approval-required) over job evidence, no second model call
- [x] 3. Connector hardening: failure backoff, missing-secret / expired-key states, dry-run output
- [x] 4. Approval reliability: idempotency-keyed approval bound to action_id, 128-bit token, retry on collision (also closes weak-nonce security gap)
- [x] 5. Cost ceiling: global daily spend cap that pauses new work
- [x] 6. Orchestrator resilience: re-acquire advisory lock + re-LISTEN after DB connection drop

## Next - capabilities
- [x] 7. MCP client P0 (per docs/mcp-support.md): config, engine tool exposure, doctor check, echo-safe (untrusted+capped output deferred to P1/source-trust)
- [x] 8. MCP server read-only: argus mcp serve exposing status/alerts/proposals/lessons
- [x] 9. Provider breadth: OpenRouter + Ollama engine paths
- [ ] 10. More channels: Discord + generic email gateway behind a capability-optional interface
- [ ] 11. Richer multi-agent / typed interactions: ask/confirm/suggest + per-agent budgets
- [ ] 12. Linux runtime parity: opinionated always-on bundle as systemd units

## Done log
- Item 1 (observability): loop.py resilient `_sweep` + `_backoff_seconds` (sweep crash logs + backs off, no silent orchestrator death); loggers in orchestrator/queue/worker/reconcile; structured logs on claim/finalize/reclaim + worker failure. Test: tests/python/v2/test_orchestrator_loop.py (5 passed). Regression: queue/fencing/reconcile/worker (26 passed).
- Item 2 (run-liveness): worker/liveness.py pure `classify()` -> produced/planning_only/blocked/approval_required/external_blocker/empty from regex evidence, no LLM. Wired into worker result + logs STUCK states. Test: tests/python/v2/test_liveness.py (15 passed). Regression: worker (6 passed).
- Item 3 (connector hardening): migration 0018 adds error_count/last_error/last_error_at/poll_after to connector_state; driver.py logs failures, records error state, exponential backoff (30s..1h) skips dead sources, clears on recovery; dry_run surfaces error_count. Test: tests/python/v2/test_connector_backoff.py (5). Checkpoint: full v2 suite 612 passed, 4 skipped.
- Item 4 (approval reliability): executor._insert_approval uses 128-bit token_hex(16) + retry-on-collision with RETURNING (guarantees an approval row exists, no more silent DO NOTHING leaving a parked action stuck). Closes the CRITICAL weak-nonce gap too. Test: tests/python/v2/test_approvals.py +2 (128-bit, collision-retry). Regression: executor/approvals/actions/pipeline 87 passed.
- Item 5 (cost ceiling): config Defaults.max_daily_cost_usd (None=off); orchestrator/budget.py sums 24h priced runs; route_events pauses opening new work when over cap (in-flight finishes), throttled warning log. Test: tests/python/v2/test_budget.py (5). Regression: reconcile (8) + config (43).
- Item 6 (orchestrator resilience): loop.py refactored to _acquire/_reacquire/_wait; on control-conn drop it reconnects (re-lock + re-LISTEN) or propagates RuntimeError handoff so a second orchestrator never double-runs; reconnect backoff capped 30s, gives up after 10 tries for supervisor restart. Test: tests/python/v2/test_orchestrator_loop.py +6. CHECKPOINT: full v2 suite 624 passed, 4 skipped. *** Reliability foundation (items 1-6) COMPLETE. ***
- Item 7 (MCP client P0): config schema McpServer/McpConfig (Config.mcp); mcp/config.py validate_server + render_claude_config + materialize; worker/exec.py materializes per-run to a temp dir (not worktree -> no diff pollution) + ARGUS_CLAUDE_MCP_CONFIG; claude_code adapter passes --mcp-config; opscheck _mcp_checks (echo-safe, no live handshake = P1); example config snippet. Test: tests/python/v2/test_mcp_config.py (12). Checkpoint: full v2 suite 636 passed, 4 skipped. (Claude Code is the MCP client; Argus renders+validates. Live protocol ping + untrusted-output caps deferred to P1.)
- Item 8 (MCP server): mcp/server.py hand-rolled stdio JSON-RPC (newline-delimited), no SDK dep; read-only tools argus_status/alerts/lessons/proposals; handle_request dispatch (initialize/tools.list/tools.call/ping/notifications); CLI `argus mcp serve`. Test: tests/python/v2/test_mcp_server.py (9). Checkpoint: full v2 suite 645 passed, 4 skipped. (Gated action tools = P2, depend on approval gate.)
- Item 9 (provider breadth): engine/adapters/openai_compat.py (stdlib urllib, no new dep) backs `openrouter` + `ollama` via OpenAI /chat/completions; registered in ADAPTERS; EngineName literal extended; env-configured (keys/model/base_url/timeout); docs/engines.md table. Test: tests/python/test_adapter_openai_compat.py (6) + updated engine-list test. Checkpoint: full suite (both dirs) 757 passed, 4 skipped.
