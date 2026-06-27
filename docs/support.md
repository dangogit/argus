# Support

Support projects poll an Apps Script transport, classify threads, draft replies,
ask for guidance when needed, and record state in Postgres.

Commands:

```bash
argus support run --team luma
argus support list --team luma
argus support reply --team luma DRAFT_ID
argus support clear --team luma DRAFT_ID
```

Replies are explicit CLI actions. If the support transport is not configured,
the command fails closed and marks the draft failed.
