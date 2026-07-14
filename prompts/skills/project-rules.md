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
every covered report or item's disposition, verification coverage and evidence, and
unresolved follow-up. Evidence: retro-change:4cd623fed543ba165d781063,
retro-change:b0879a7567c1ac24f9977dbf, retro-change:2c0d2299d85d2858e4aa6f67,
retro-change:5f423b90d23f1eb832787065, retro-change:e3d046e4c372455df82bfe30.
