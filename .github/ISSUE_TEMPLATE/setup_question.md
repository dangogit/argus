---
name: Setup question
about: Get help with install, onboarding, Slack, Telegram, connectors, or go-live
title: "[setup] "
labels: question
assignees: ""
---

Prefer GitHub Discussions for setup help:
https://github.com/dangogit/argus/discussions/new/choose

**Goal**
What are you trying to make operational?

**Install path**
- OS:
- Install path: (pipx / uv / source checkout)
- `argus --version`:

**Onboarding mode**
- Mode: (`chat-only` / `monitor-only` / `pm-propose-pr`)
- Channel: (Slack / Telegram / CLI / other)
- Engine: (`echo` / `codex` / `claude-code` / `hermes`)

**Current status**
Paste redacted output:

```text
argus doctor --deep --live --json
argus go-live --mode chat-only --public-url https://example.com/slack
```

**Redaction checklist**
- [ ] No tokens, passwords, signing secrets, or webhook secrets.
- [ ] No private customer data.
- [ ] No full `.env` files.
- [ ] Private repo names and Slack user IDs redacted if needed.
