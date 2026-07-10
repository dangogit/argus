---
name: project-rules
triggers: [fix, bug, broken, failing, failure, error, issue, implement, change, review, diff, qa]
roles: [developer, qa, senior]
---
This is repo work. Before editing or reviewing, read repo-local rules when present:

- `AGENTS.md`
- `.agents/skills/*/SKILL.md` only when the task clearly matches that skill

Apply global owner rules from the prompt. If repo rules conflict with owner rules, owner rules win. Keep the change small and verify with the narrowest relevant check.

QA-sensitive work cannot close unless the transcript records the access path,
covered items, disposition for every covered report or item, verification evidence,
and unresolved follow-up condition.

Evidence basis: retro-change:4cd623fed543ba165d781063,
retro-change:bb3a1f7887584ccc49e85f87,
retro-change:2c0d2299d85d2858e4aa6f67,
retro-change:2c1f44510a3ec90e2eda8cd6.
