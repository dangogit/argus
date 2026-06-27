# Threat Model

Primary boundaries:

- **Secrets:** configs should reference environment variables or env files.
- **Inbound chat:** receiver secrets, channel bindings, and owner allowlists gate
  WhatsApp and Telegram messages before work is opened.
- **Actions:** actions are classified as reversible internal, personal outward,
  or irreversible outward before execution.
- **PM edits:** PM work runs in isolated worktrees and proposes PRs. It does not
  merge or deploy by default.
- **Publishing and support replies:** both require explicit commands and fail
  closed when transports are missing.
- **Calendar:** write actions require configured credentials and pass through
  the action risk policy when proposed by agents.

Run `argus doctor`, `argus ready`, and `python scripts/gate.py` before live
cutovers.
