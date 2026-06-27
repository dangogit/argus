---
name: simplify-change
triggers: [simplify, refactor, cleanup, clean up, duplicate, duplication, dead code, tech debt, over-engineered, dry]
roles: [developer, qa]
---
This request asks to simplify or clean up code, not to add behavior. Make the diff smaller, not bigger.

Example: three call sites each format a price inline -> extract one `format_price(cents)` helper and call it; delete the three copies. Net lines go down, behavior is identical.

Discipline:

- Chesterton's Fence: do not delete or collapse code you cannot explain. If a branch, guard, or odd-looking line has no obvious reason, leave it and say why in the PR, do not assume it is dead.
- Behavior must stay identical. A refactor that changes output is a bug, not a cleanup. Lean on existing tests; if none cover the touched path, add one before refactoring.
- Never swallow an error to make code look cleaner. Removing a `try`/`except` or a null check is a behavior change, not simplification.
- No slop: do not add abstraction, config, or indirection that the task did not ask for. The win is fewer lines and fewer concepts, not a prettier framework.
- Keep it minimal and reviewable. Prefer several small, obvious deletions over one large rewrite.
