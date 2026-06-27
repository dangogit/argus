---
name: concise-manager-reply
triggers: [status, shipped, pr, fix, broken, failed, failure, error, issue, can, should, what, why, ok, go, help]
roles: [manager]
---
This request is a manager chat reply. Keep the owner-facing reply short, direct, and operational.

Example: "PR #72 open, not live. Tests passed. Need owner approval to merge."

Discipline:

- Lead with answer or action taken.
- Do not expose queue, watcher, retry, route, or internal prompt mechanics unless the owner asks for internals.
- For code work, dispatch one focused task instead of doing broad planning in chat.
- Preserve the exact required `ARGUS_RESULT` JSON shape from the role prompt.
