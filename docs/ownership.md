# Persistent Team Ownership

Persistent ownership makes a team responsible for closing work across process
boundaries. It is disabled by default. Enabling it does not grant broad
authority. Each side effect still passes through typed policy, evidence checks,
and the normal action executor.

## Durable Concepts

Argus keeps four related records because each answers a different question:

| Record | Question it answers | Completion means |
|---|---|---|
| Request | What outcome did a signal or operator ask for? | The role pipeline reached a request result. |
| Job | What executable role work is queued or running? | One worker attempt finished or exhausted its retry policy. |
| Action | What side effect should happen? | The action was approved when required and the provider call returned. |
| Obligation | Does the team still owe the real-world outcome? | The configured definition of done has provider evidence. |

A completed request is not proof that its PR merged. A completed merge action is
not proof that the exact commit deployed. A support transport call is not proof
when its response was lost. The obligation remains open across those boundaries
and links the source, request, latest action, provider reference, definition of
done, attempts, and accumulated evidence. Every state change also appends an
event to `team_obligation_events`.

## States And Legal Transitions

| State | Meaning | Legal next states |
|---|---|---|
| `open` | Accepted but not started. | `working`, `awaiting_approval`, `blocked`, `failed` |
| `working` | Pipeline or domain work is active. | `awaiting_pr`, `awaiting_deploy`, `verifying`, `awaiting_approval`, `blocked`, `failed` |
| `awaiting_pr` | Waiting for the linked `open_pr` action and canonical PR inspection. | `awaiting_merge`, `blocked`, `failed` |
| `awaiting_merge` | PR exists and merge policy is being evaluated. | `awaiting_deploy`, `verifying`, `awaiting_approval`, `blocked`, `failed` |
| `awaiting_deploy` | Exact merge commit is waiting for the configured deployment provider. | `verifying`, `blocked`, `failed` |
| `verifying` | Provider outcome or staging HTTP behavior is being checked. | `working`, `blocked`, `done`, `failed` |
| `awaiting_approval` | A policy-controlled action needs an operator decision. | `working`, `awaiting_pr`, `awaiting_merge`, `awaiting_deploy`, `verifying`, `blocked`, `failed` |
| `blocked` | Safe automatic progress is impossible. | `open`, `working`, `awaiting_approval`, `failed` |
| `done` | Definition of done is proven. | None |
| `failed` | Terminal failure. | None |

Repeating the same state with new evidence is allowed and is recorded. `done`
and `failed` are terminal. A blocked obligation is not automatically retried.

Definition of done is kind-specific:

- Code and maintenance work require a real PR in the configured repository, a
  canonical merge commit, a successful GitHub workflow or Vercel deployment
  for that exact commit, and 2xx responses from every configured smoke path.
- Support requires the configured transport calls to return and a provider
  reply reference to be recorded. If delivery is ambiguous, Argus blocks
  because the provider cannot prove exactly-once delivery.
- Maintenance starts only from current durable evidence. After dispatch it uses
  the same PR, deploy, and smoke proof as code work.

## Configuration And Gates

Team ownership and automatic actions default off. Support coverage defaults on
inside an enabled ownership policy so existing support teams keep their inbox:

- `ownership.enabled: false`
- `ownership.code.auto_ready: false`
- `ownership.code.auto_merge: false`
- `ownership.code.deploy_provider: github`
- `ownership.support.enabled: true`
- `ownership.support.auto_send_low_risk: false`
- `ownership.maintenance.enabled: false`

Use explicit action overrides. `owner prove` reports a missing override even
when the risk-wide fallback would also require approval.

This staging-first example makes draft PRs ready automatically, but keeps merge
and support replies approval-gated:

```yaml
teams:
  - name: app
    autonomy:
      actions:
        ready_pr: auto
        merge_pr: approval
        support_reply: approval
    project:
      repo: /absolute/path/to/app
      github_repo: owner/app
      base_branch: staging
      work_branch_prefix: argus/app
    ownership:
      enabled: true
      code:
        auto_ready: true
        auto_merge: false
        allowed_base_branches: [staging]
        required_checks: [Unit & Integration Tests]
        deploy_workflow: Deploy to Staging
        live_url: https://staging.example.com
        smoke_paths: [/]
      support:
        auto_send_low_risk: false
      maintenance:
        enabled: false
    channels:
      - { type: slack, role: control, channel_id: C1234567890 }
    sources:
      - name: app-support
        type: support_apps_script
        team: app
        secret_ref: ${env:APP_SUPPORT_KEY}
        config:
          url: https://script.google.com/macros/s/DEPLOYMENT_ID/exec
```

Use the exact GitHub check and workflow names. The project `base_branch` must be
allowlisted. `main`, `master`, `production`, and `prod` can never be automatic
merge targets. Automatic ready or merge also requires:

- an open PR in the configured GitHub repository;
- the configured work branch prefix and a full immutable head SHA;
- a clean merge state and all configured required checks successful;
- changed-file evidence with no mandatory or configured blocked path;
- the matching `ready_pr` or `merge_pr` action set to `auto`.

Mandatory blocked paths include CI workflows, env and credential files,
migrations, auth, billing, payments, cloud functions, package manifests and
lockfiles, and Firebase rules. Operators may add stricter `blocked_globs`.

For a project deployed by Vercel Git integration, replace `deploy_workflow`
with an exact project and scope. Argus queries Vercel by the immutable merge
commit SHA and configured base branch, then verifies the returned project,
scope, commit metadata, and deployment URL before smoke testing:

```yaml
ownership:
  enabled: true
  code:
    allowed_base_branches: [staging]
    required_checks: [CI]
    deploy_provider: vercel
    deploy_project: app
    deploy_scope: company-team
    deploy_vercel_auth: cli
    live_url: https://app-git-staging-company-team.vercel.app
    smoke_paths: [/]
  support:
    enabled: false
```

Set `ownership.support.enabled: false` only when that team has no support
inbox. This removes the support source prerequisite and prevents support
obligations or automatic replies. It does not weaken code ownership gates.

Use `deploy_vercel_auth: cli` on a local owner host that should use its Vercel
CLI login. This ignores an ambient `VERCEL_TOKEN` for the read-only deployment
inspection. The default, `environment`, keeps token-based automation behavior.

Low-risk support auto-send additionally requires exactly one team-bound Apps
Script support source with a URL and secret, `support_reply: auto`, confidence
at or above `min_confidence`, `risk: low`, no guidance or escalation flag, and
no sensitive category or term in sender, subject, thread, or reply. Billing,
refunds, payments, charges, account access, passwords, login, ownership,
security, privacy, legal, deletion, disputes, and normalized variants block.

Maintenance dispatch requires ownership and maintenance enabled, a project,
capacity under both open limits, the configured interval elapsed, and current
evidence from a configured connector, failed Argus request, or failed draft PR.
The evidence must identify its team, source, timestamp, severity, and message.
Argus dispatches at most one highest-priority eligible candidate and tells the
worker not to invent adjacent work.

## Operator Commands

Run one cycle for all enabled teams or one team:

```bash
argus owner cycle --json
argus owner cycle --team app --json
```

The command reconciles due code-producing obligations, collects one eligible
maintenance candidate per team, processes proposed actions, and commits once.
Exit `0` means the cycle completed, including when a specific obligation became
blocked. Exit `1` means the cycle itself failed and was rolled back. An unknown
team exits `2`.

List summary fields without exposing obligation evidence or customer content:

```bash
argus owner list --team app --json
argus owner list --team app --status blocked --limit 50 --json
```

`--status` accepts any state in the table. `--limit` accepts 1 through 500.
`owner list` exits `0` on a successful read, `1` on a runtime or database error,
and `2` for an unknown team or invalid CLI argument.

Prove policy wiring before enabling the timer:

```bash
argus owner prove --team app --json
```

Exit `0` and `"ready": true` mean the policy wiring is complete. Exit `1`
means `missing_prerequisites` is non-empty or the proof could not run. Exit `2`
means the team or arguments are invalid. The output reports action modes,
allowlisted and protected branches, required checks, deploy workflow, redacted
live smoke target, support transport readiness, maintenance settings, and due
or blocked counts. It is read-only and does not prove that a future provider
operation will succeed.

`argus doctor --deep --live --json` proves dependencies such as engines,
channels, repo access, executables, and connector dry-runs. `owner prove` proves
that ownership policy is explicitly and coherently wired. Run both.

## Evidence And Recovery

For code work, treat an obligation as proven only when its evidence identifies:

1. the canonical PR URL and number in the configured repository;
2. the inspected head SHA and resulting merge SHA;
3. the exact workflow name, run ID, URL, status, and successful conclusion for
   that merge SHA;
4. a timestamped 2xx result for every configured staging smoke URL.

Use the provider links, then compare the deployed staging behavior yourself
when the change needs semantic or visual verification. The smoke check proves
HTTP availability, not product correctness.

Start blocked-work recovery with read-only inspection:

```bash
argus owner list --team app --status blocked --json
argus runs REQUEST_ID
argus actions REQUEST_ID
argus owner prove --team app --json
```

Correcting the recorded cause does not resume the blocked row. There is
intentionally no generic `owner retry` or `owner unblock` command, and blocked
obligations are not selected by `owner cycle`. Do not edit obligation rows
directly, because that bypasses the append-only event trail and legal transition
checks.

Use only a domain flow that already implements a legal transition. A successful
explicit support send through the normal support flow can reconcile its matching
obligation. An approval nonce applies to an `awaiting_approval` obligation, not
a blocked one. Blocked code or deploy obligations currently require operator
escalation and a genuinely new source item. They cannot resume until a public
retry or unblock flow exists.

For an ambiguous support delivery, first inspect the provider thread. Never run
`argus support reply` or the action again merely because Argus timed out. If the
reply exists, leave the thread alone. The current public CLI does not expose a
provider-only reconcile command, so the obligation remains blocked and requires
operator escalation. If provider inspection proves the reply does not exist,
obtain explicit operator authorization before sending a new reply through the
normal support flow. The provider transport does not offer an exactly-once
guarantee.

To prove a customer reply, verify the exact thread in the provider and confirm
the obligation is `done` with the matching `support:THREAD_ID` provider
reference. `owner list` intentionally omits private evidence, so use
operator-controlled database or dashboard access for that detailed audit.

## Rollout Levels

1. **Shadow:** enable ownership with all three action overrides set to
   `approval`, keep `auto_ready`, `auto_merge`, and support auto-send false,
   keep maintenance disabled, and inspect `owner prove`, cycles, and blocked
   reasons.
2. **Staging:** allow `ready_pr: auto` and `auto_ready: true` on one staging
   branch. After reliable observation, optionally allow `merge_pr: auto` plus
   `auto_merge: true` for that staging branch. Require exact checks, workflow,
   live URL, and smoke paths.
3. **Production:** keep production merges and deploys in the existing explicit
   approval flow. Ownership automatic merge rejects protected production branch
   names even if configured.

Change one authority boundary at a time. A green shadow cycle proves wiring,
not production safety.

## Hard Boundaries

Argus will never automatically:

- merge an ownership PR to `main`, `master`, `production`, or `prod`;
- merge a PR that fails repository, branch, SHA, check, mergeability, or path
  policy;
- claim deployment from a PR or branch alone instead of the exact successful
  workflow run for the merge commit;
- send sensitive or ambiguous support, or retry an uncertain outward delivery;
- edit secrets, expose configured secret values in owner command output, or
  grant an agent secret-management authority;
- grant authority for destructive or unclassified outward work through the
  ownership loop;
- invent maintenance work without current durable evidence;
- follow smoke redirects to another host or a private network address.

See [Configuration](configuration.md), [Live Onboarding](live-onboarding.md),
and [Operations](operations.md) for setup and scheduling.
