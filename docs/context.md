# Context

Context distills facts, extracts commitments, and stores commitment state in
Postgres.

Commands:

```bash
argus context distill
argus context remind
argus context status
argus context commitments
argus context done COMMITMENT_ID
argus context snooze COMMITMENT_ID --until 2026-06-20T09:00:00Z
argus context dismiss COMMITMENT_ID
argus context recall "invoice"
```

Facts are written to the configured vault path. Commitments, reminder batches,
and watermarks are in Postgres.
