# Project Memory

Argus keeps one team-scoped project brief for manager conversations. The brief
is a read-only projection of current requests, semantic daily summaries,
validated PM lessons, scoped knowledge, recent outcomes, Argus-created pull
requests, and pending retro items.

## Commands

Refresh the previous two UTC days for every configured team:

```bash
argus memory refresh
```

Refresh a wider retry window, one team, or one exact UTC day:

```bash
argus memory refresh --lookback-days 3
argus memory refresh --team dev
argus memory refresh --team dev --day 2026-07-13
```

Inspect the brief or its stable JSON form:

```bash
argus memory brief --team dev
argus memory brief --team dev --json
argus memory status
argus memory status --team dev
```

`argus summarize` and `argus history` remain available for compatibility.

## Storage and quality

Daily rows remain in `conversation_summaries`. Migration `0029` adds the
structured `details`, `source_fingerprint`, and `updated_at` fields. Quality is
reported as:

- `semantic`: every selected source chunk produced valid structured memory.
- `partial`: at least one chunk succeeded and at least one failed validation or
  engine execution.
- `fallback`: no semantic chunk was usable, so Argus stored deterministic
  activity counts.
- `unchanged`: an existing semantic summary already matches the source
  fingerprint, or the requested day has no activity.

Fallback and partial days are retried by later refreshes. A matching semantic
summary is never replaced by a lower-quality result. Evidence references must
resolve to a team-scoped event, request, or action.

Before model execution and persistence, Argus removes common prompt-injection
phrases and redacts bearer tokens, credential assignments, AWS access keys, and
private-key blocks. The summary engine runs without tools.

## Scheduling and logs

Generated launchd and systemd bundles include one daily `memory refresh` unit
with an 86,400-second interval:

- launchd label: `com.argus.memory`
- systemd timer: `argus-memory.timer`
- default log: `memory.log` in the configured Argus log directory

Render and manually inspect briefs before installing or enabling the generated
unit. After enabling it, review `memory.log` daily for fallback rates, invalid
evidence, and team-scope errors.
