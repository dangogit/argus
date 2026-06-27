---
name: project-rules
triggers: [fix, bug, broken, failing, failure, error, issue, implement, change, review, diff, qa]
roles: [developer, qa, senior]
---
This is repo work. Before editing or reviewing, read repo-local rules when present:

- `AGENTS.md`
- `.agents/skills/*/SKILL.md` only when the task clearly matches that skill

Apply global owner rules from the prompt. If repo rules conflict with owner rules, owner rules win. Keep the change small and verify with the narrowest relevant check.
