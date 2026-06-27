## What and why

Briefly: what this changes and the motivation.

## Checklist

- [ ] `python scripts/gate.py` passes for Python changes
- [ ] Dashboard changes pass `npm run test` and `npm run build`
- [ ] New behavior is covered by focused tests
- [ ] Docs updated if behavior or config changed (`docs/`, `README.md`)
- [ ] No secrets, tokens, phone numbers, or private repo data in the diff
- [ ] No em dashes in code, comments, or docs (project convention)
- [ ] For a new connector or channel: fixture -> dry-run -> live smoke contract
      followed, and unsupported config fails validation clearly

## Notes for reviewers

Anything worth calling out: trade-offs, follow-ups, areas needing a careful look.
