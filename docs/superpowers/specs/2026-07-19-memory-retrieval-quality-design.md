# Memory Retrieval Quality Design

## Goal

Improve Argus memory reliability using three ideas validated during the MDBrain
review: operational auditing, measurable retrieval quality, and hybrid search.
Keep Postgres as the only durable store. Preserve existing memory, knowledge,
and project-brief interfaces.

## Current State

Argus already provides:

- team-scoped project memory briefs;
- evidence validation for semantic summaries;
- prompt-injection filtering and secret redaction;
- stale-summary exclusion;
- company and team knowledge scopes;
- pgvector embeddings with keyword fallback;
- twenty project-memory regression fixtures covering recall, non-recall,
  isolation, and safety.

Current gaps:

- operators cannot distinguish a healthy memory system from one that never ran;
- knowledge search returns vector results without considering keyword results;
- regression cases pass or fail individually but do not produce a quality
  report;
- releases have no explicit memory-quality gate beyond ordinary test success.

## Scope

### 1. Read-Only Memory Audit

Add `argus memory audit`, with optional `--team` and `--json` arguments.

Audit every configured team by default. Read only from Postgres. Use a fixed
fourteen-day activity window, matching the project brief's durable-memory
horizon. Report:

- memory-source activity count and latest activity time across message events,
  requests, and actions;
- active UTC days without a corresponding team summary;
- summary count, latest summary day, quality, and age;
- whether memory refresh has ever produced a summary;
- invalid or cross-team evidence references in semantic summary details;
- team knowledge count and applicable company knowledge count;
- embedding coverage for applicable knowledge;
- exact duplicate knowledge count after normalized content comparison;
- malformed scope rows, including team knowledge without a team and company
  knowledge with a team;
- overall state: `ready`, `degraded`, or `not_ready`.

Summary counts, quality, and evidence checks use the fourteen-day window.
`ever_produced_summary` checks full history so operators can distinguish an
idle team from a memory job that never succeeded.

Global knowledge findings appear once in a top-level audit section. Team
sections contain only team-specific findings plus applicable company knowledge
counts. Duplicate content is advisory because separate sources may knowingly
record the same rule. Global state covers malformed scope rows and company
embedding gaps. Team state covers that team's summary, evidence, and embedding
findings. Overall state is the worst state across global and team sections, with
`not_ready` taking precedence over `degraded`.

State rules:

- `not_ready`: one or more active days in the audit window have no summary;
- `degraded`: latest in-window summary is fallback or partial, evidence is
  invalid, embeddings are missing, or scope rows are malformed;
- `ready`: team has no audit failures and no unsummarized active days;
- informational age and counts do not fail readiness unless they expose one of
  the conditions above.

Text output stays compact and line-oriented. JSON output uses stable field names
and includes every measured count. Command exits `0` only when every selected
team is ready, `1` when any team is degraded or not ready, and `2` for invalid
CLI input or configuration.

### 2. Hybrid Knowledge Retrieval

Keep `argus.v2.knowledge.store.search()` public inputs and returned
`{"title", "content"}` records unchanged.

When query embedding is available:

1. Fetch up to `max(k * 4, 20)` vector candidates.
2. Fetch same number of lexical candidates using existing content FTS plus
   exact title and title/content substring matching.
3. Fuse both ranked lists using reciprocal rank fusion with a fixed constant of
   60.
4. Deduplicate by knowledge row ID.
5. Sort by fused score, best individual-lane rank, newest creation time, then
   stable row ID, and return top `k`.

When embedding fails or is unavailable, use lexical results. Scope and source
filters must be identical in every lane.

Search must never broaden visibility. A team sees company knowledge plus its
own team knowledge only. Result scores and internal row IDs remain private so
existing prompt rendering stays unchanged.

### 3. Memory Evaluation Harness

Add one test-only evaluation harness under `tests/python/v2`. Keep evaluation
plumbing out of production modules. Its single interface seeds a case in
isolated test Postgres, executes the real memory or knowledge interface, and
returns a deterministic report for all cases.

Metrics:

- total, passed, and failed cases;
- pass rate;
- hit rate for required recall;
- forbidden-recall failures;
- scope-leak failures;
- stale-recall failures;
- safety failures;
- invalid-evidence failures;
- average and p95 latency.

Extend fixture metadata with explicit tags where needed. Keep current expected
and forbidden phrase assertions as source truth. Add focused knowledge-search
cases for semantic queries, exact identifiers, source filters, mixed company and
team visibility, and foreign-team exclusion.

Evaluation release gate passes only when:

- every case passes;
- scope-leak failures equal zero;
- stale-recall failures equal zero;
- safety failures equal zero;
- invalid-evidence failures equal zero.

Latency is reported but not gated in this first version because CI and local
Postgres timing varies.

## Module Interfaces

`argus.v2.memory.audit.run(conn, cfg, team_ids, now) -> MemoryAuditReport` is a
deep module interface used by CLI and tests. It owns SQL, evidence validation,
state classification, and stable report construction. CLI owns only argument
parsing, text or JSON rendering, and exit-code mapping.

`argus.v2.knowledge.store.search()` remains the only production retrieval
interface. Vector, lexical, fusion, and fallback helpers stay internal to that
module. No new adapter or public scoring interface is added.

Test evaluation exposes one test-helper interface that runs complete cases.
Individual tests assert on report outcomes, not internal ranking or audit SQL.

## Data Flow

Memory audit reads existing tables and emits a per-team report without writes.
Knowledge retrieval executes vector and lexical candidate queries against the
same scope, fuses ranks in Python, and returns the existing result shape.
Evaluation tests seed isolated test Postgres through existing fixtures, call
real brief and knowledge retrieval paths, then score returned output through
the test-only harness.

No production or live database is seeded by evaluation code.

## Error Handling

- Audit query failure returns a clear error and nonzero exit. It must not label
  an unreadable system healthy.
- One malformed summary detail is counted and surfaced without crashing the
  remaining team audit.
- Embedding provider failure logs a warning and falls back to lexical
  retrieval.
- Lexical-query and database connection failures propagate. Retrieval must not
  hide an unreadable database behind empty results.
- Evaluation treats unclassified exceptions as failed cases, never passes them
  silently.

## Testing

Use test-driven development for every behavior.

Targeted coverage:

- audit ready, degraded, not-ready, JSON, exit-code, team-filter, evidence,
  duplicate, embedding, and malformed-scope cases;
- hybrid ranking, cross-lane deduplication, stable ties, lexical-only fallback,
  exact-title recall, source filters, company visibility, and team isolation;
- evaluation metrics, p95 calculation, failure categories, and release-gate
  decision through the complete harness interface;
- CLI parser and output compatibility;
- existing project-memory and knowledge integration tests.

Final verification:

```bash
python -m pytest tests/python/v2/test_memory_audit.py \
  tests/python/v2/test_memory_eval.py \
  tests/python/v2/test_knowledge_integration.py \
  tests/python/v2/test_cli.py -q
python scripts/gate.py
```

Live verification remains read only:

```bash
argus memory audit --json
argus memory status
```

## Non-Goals

- MongoDB, Atlas Search, Voyage-specific features, or another durable store;
- new knowledge or claim tables;
- graph traversal;
- automatic contradiction detection;
- Obsidian, GitHub, Slack, Notion, Confluence, or CRM connectors;
- automatic mutation or cleanup of live memory rows;
- latency-based release failure in this version.

## Success Criteria

- Operators can prove whether memory has run and whether stored memory is safe.
- Audit finds every unsummarized active day in the durable-memory horizon.
- Exact and semantic knowledge queries both influence final ranking.
- Existing callers receive unchanged result shapes.
- Evaluation produces stable quality metrics and fails closed on safety or
  isolation regression.
- No migration, dependency, secret, or second storage system is added.
- Targeted tests and full Argus gate pass.
