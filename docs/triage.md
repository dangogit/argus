# Connectors And Signals

Signals enter through polling connectors or webhooks and become durable events.

Built-in connector types:

- `fake`
- `sentry`
- `github`
- `uptime`
- `vercel`
- `vercel_events`
- `supabase`
- `firebase`
- `posthog`
- `postgres`
- `email_imap`
- `fly`
- `webhook`

Commands:

```bash
argus poll --dry-run
argus doctor --deep --json
argus go-live --mode monitor-only --public-url https://argus.example.com/slack --skip-connector sentry-main
argus poll --source sentry-main
argus signal --team dev --source manual --fingerprint ISSUE-1 '{"severity":"warn"}'
argus alert add --severity warn --project dev --fingerprint ISSUE-1 --message "Fix login"
argus alert list --project dev
```

Connector cursors live in Postgres. A failing source rolls back only its own
poll transaction and does not block other sources.

`argus onboard project PATH` detects provider hints but does not enable
connectors just because a repo mentions Vercel, Supabase, Sentry, Firebase, or
another provider. Add a source only after the matching env token exists in a
private env file. Then run `argus doctor --deep --json` and
`argus go-live --mode monitor-only`.

Vercel sources can use either a stable `secret_ref` such as
`${env:VERCEL_TOKEN}` or local Vercel CLI auth. If `secret_ref` is omitted,
Argus reads the Vercel CLI `auth.json` file from `vercel login` and refreshes
the token when needed. Set `config.auth_file` only when the auth file lives in a
non-standard path. For servers, prefer a dedicated `VERCEL_TOKEN`.

Use `vercel` for failed deployments. Use `vercel_events` for production 5xx
runtime events when Sentry or PostHog API tokens are not available:

```yaml
sources:
  - type: vercel_events
    name: vercel-my-project-runtime-errors
    config:
      project: prj_123
      team: team_123
      target: production
      status_code: 5xx
```

If `config.deployment` is omitted, Argus resolves the latest READY production
deployment before polling events.

Sentry source example:

```yaml
sources:
  - type: sentry
    name: sentry-my-project-issues
    secret_ref: ${env:SENTRY_AUTH_TOKEN}
    config:
      org: ${env:SENTRY_ORG}
      project: ${env:SENTRY_PROJECT}
      min_level: error
```

PostHog error-tracking source example:

```yaml
sources:
  - type: posthog
    name: posthog-my-project-errors
    secret_ref: ${env:POSTHOG_PERSONAL_API_KEY}
    config:
      host: ${env:POSTHOG_HOST}
      project: ${env:POSTHOG_PROJECT_ID}
      endpoint: error_tracking
      project_name: my-project
```

Postgres table source example:

```yaml
sources:
  - type: postgres
    name: postgres-my-project-webhook-errors
    secret_ref: ${env:MY_PROJECT_DATABASE_URL}
    config:
      table: webhook_errors
      cursor_column: createdAt
      fingerprint_column: id
      cursor_type: timestamp
      initial_cursor: '2026-06-25T00:00:00+00:00'
```

Identifiers are validated and quoted, so Prisma-style camelCase columns like
`createdAt` work. Use `initial_cursor` when attaching Argus to an existing
production table so first poll does not ingest old rows.

Do not treat `SENTRY_DSN`, `NEXT_PUBLIC_SENTRY_DSN`, or
`NEXT_PUBLIC_POSTHOG_KEY` as operational connector credentials. They let the
app emit telemetry, but Argus still needs provider API credentials to poll.
If a deployment requires these providers, run live checks with
`--require-source-type sentry` or `--require-source-type posthog` so a missing
source blocks `operational` status instead of passing silently.
For one project, use `--require-team-source-type team:type`. For all-project
coverage, use `--require-each-team-source-type sentry` and
`--require-each-team-source-type posthog` so one configured source cannot
satisfy every project.

Firebase sources can use either a `secret_ref` with a bearer token or local
Firebase CLI auth. If `secret_ref` is omitted, Argus reads
`firebase-tools.json` from `firebase login` and refreshes the access token when
needed. Set `config.auth_file` only when that file lives in a non-standard path.

Deep connector statuses:

- `ok`: configured and dry-run passed.
- `missing_secret`: a referenced env secret is not available.
- `auth_failed`: the connector ran but the provider rejected or failed it.
- `skipped`: passed explicitly with `--skip-connector`.
- `not_configured`: no connector source is configured for this install.
