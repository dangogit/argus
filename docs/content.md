# Content

The content subsystem queues briefs, generates drafts, and publishes only when
a publisher is configured and explicitly invoked.

Commands:

```bash
argus content draft --project luma --platform linkedin --request "announce launch"
argus content drain
argus content list
argus content publish DRAFT_ID
```

Publishing requires:

```bash
export ARGUS_CONTENT_PUBLISH_ENABLED=1
export ARGUS_SOCIAL_PUBLISH_COMMAND="..."
```

Without those settings, publishing fails closed.
