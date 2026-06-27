# Retro

Retro is the daily learning loop. It reviews completed team work, writes
improvement backlog items, bridges safe team lessons into PM memory, and
bridges company lessons into company knowledge.

Roles:

- Team Learning Agent: per-project Retro Facilitator.
- Company Learning Agent: company Chief of Staff.

Commands:

```bash
argus retro run [--team TEAM] [--company-only] [--no-notify]
argus retro notify [--team TEAM] [--company-only]
argus retro backlog [--team TEAM] [--status gated]
argus retro summary
```

The retro job is included in generated launchd runtime units and can also be
scheduled through manifest-based host jobs.

`argus retro run` queues notification digests by default:

- Team PM digest: one message to each project team's control channel with that
  team's lessons, improvement candidates, infra notices, quarantines, and
  auto-change request count.
- CEO retro brief: one message to the `ceo-brief` team control channel with
  company-level lessons and cross-team improvement candidates.

Use `--no-notify` for a silent learning run. Use `argus retro notify` to queue
digests from already-recorded retro data.

Config:

```yaml
retro:
  authority: propose        # propose | auto-changes
  company_change_team: dev  # optional target for company-level auto-change PR work
```

`auto-changes` only opens internal PM requests for evidence-backed `skill`,
`prompt-edit`, or `process-edit` candidates. Existing approval gates still
control merge, deploy, outward messages, destructive work, and secrets.

## Promotion Rules

- `lesson`: team lessons bridge to PM memory; company lessons bridge to
  company knowledge.
- `skill`, `prompt-edit`, and `process-edit`: eligible for auto-change only
  when `authority: auto-changes`, confidence is at least `0.7`, impact is at
  least `7`, and there are at least 3 evidence ids.
- Company auto-changes also need the same theme in at least 2 teams, or at
  least 4 total evidence ids.
- `infra-flag`: backlog only.
- Prompt-injection, secret, destructive, and remote shell install patterns are
  quarantined.

## Storage

- `retro_records`: raw team and company learning records.
- `retro_backlog`: gated, quarantined, infra-notice, and auto-change queue.
- `pm_lessons`: project lessons injected into PM prompts.
- `knowledge(scope='company')`: company lessons available to all teams.
- `actions(type='notify')`: team PM digests and CEO retro briefs queued through
  existing channel, quiet-hour, and approval policy.
