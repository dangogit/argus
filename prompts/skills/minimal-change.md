---
name: minimal-change
triggers: [fix, bug, broken, failing, failure, error, issue, regression, implement, change]
roles: [developer, qa]
---
This request asks for code work. Make the smallest correct change that fixes the real cause.

Example: three callers crash on the same bad input because one shared parser trusts shape. Fix the parser once and cover it with the narrowest failing test. Do not add guards to all three callers.

Discipline:

- Read the touched flow first. Grep callers before changing a shared function.
- Prefer existing helpers, stdlib, platform features, and installed dependencies over new code.
- Do not add abstraction, config, factories, or future-proofing unless the current bug or feature needs it.
- Fix root cause when tractable. Do not hide symptoms with local guards if callers share one boundary.
- Leave one focused verification: targeted test, lint, typecheck, or command that would fail if the change breaks.
