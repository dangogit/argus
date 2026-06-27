# Skills (load-on-relevance prompt playbooks)

Drop a `*.md` file here (or in a dir pointed to by `$ARGUS_SKILLS_DIR`, or a
per-team skills dir) to make a skill available. A skill is injected into an
agent's prompt **only when the incoming text matches one of its triggers**, so
prompts stay bounded: a no-match contributes zero tokens.

Files whose name starts with `_` (like this one) are ignored by the loader.

## Format

```markdown
---
name: pr-review
triggers: [review, diff, lint, pr]
roles: [judge, senior]   # optional; omit/empty => any role
---
When reviewing a diff: check error handling at trust boundaries, confirm tests
cover the changed branch, flag any secret/credential in the patch. Keep findings
to one line each: `path:line: severity: problem. fix.`
```

- **name**: unique id. A role opts in via its `skills:` list in config; if a
  role lists no skills, every loaded skill is a candidate (filtered by `roles`).
- **triggers**: lowercased keywords; selection scores by how many appear in the
  message. Highest score wins, ties broken by name. Bounded to `max_k` (default 2).
- **roles**: restrict which roles may receive the skill. Empty => any role.

## Design rules (from Anthropic's context-engineering guidance)

- If you can't state in one sentence when a skill fires, it isn't ready.
- Merge overlapping skills: overlap confuses selection the same way overlapping
  tools confuse tool choice.
- Prefer fewer, denser skills. Every injected token competes with context;
  context rot is real and cumulative.
- Lead with one concrete example, not an exhaustive rule list.

## Where it's injected

- **Pipeline / converse workers**: selection is frozen into the job's
  `exec_snapshot["skills"]` at enqueue time (deterministic replay).
- **Advisor** and **assistant-memory**: selected inline at prompt-build time.
