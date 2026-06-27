# Advisor

Advisor is a public-group WhatsApp helper. It stores group messages, replies,
cursors, attempts, and digests in Postgres.

Commands:

```bash
argus advisor ingest --group 120@g.us --message-id M1 --participant 111 "hello"
argus advisor tick
argus advisor digest
argus advisor status
```

The tick path is mention-gated, rate-limited, and tool-less. Long replies are
split into capped WhatsApp bubbles.
