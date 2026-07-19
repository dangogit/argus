# Operations

Health checks:

```bash
argus doctor
argus ready
argus status
argus verify
argus owner prove --team TEAM --json
```

Host jobs:

```bash
argus launchd render --out /tmp/argus-launchd --env-file /path/to/runtime.env
argus launchd render --os linux --out /tmp/argus-systemd \
  --env-file /path/to/runtime.env
argus host render --os linux --jobs-dir ./jobs --out /tmp/argus-extra-jobs
argus host status
argus host watchdog
argus host backup
argus host logrotate
```

`argus launchd render` emits the built-in always-on runtime jobs:

- `serve`
- `up`
- `work-chat`
- `work-pipeline`
- `poll`
- `owner` every 300 seconds
- `retro` every 86400 seconds
- `memory` every 86400 seconds
- `watchdog`
- `backup`
- `logrotate`

On macOS these are `com.argus.*` launchd units. On Linux the same renderer
emits `argus-*.service` files plus timers for scheduled jobs, including
`argus-owner.timer`. `argus host render` is a separate renderer for additional
YAML job manifests and does not create the built-in runtime bundle.

The owner timer runs `argus owner cycle --json`. Monitor policy and durable
work with:

```bash
argus owner prove --team TEAM --json
argus owner list --team TEAM --json
argus owner list --team TEAM --status blocked --json
argus owner cycle --team TEAM --json
```

`owner prove` checks policy wiring, not runtime dependencies. Pair it with
`argus doctor --deep --live --json`. A successful cycle may report blocked
obligations and still exit `0`. Its `blocked` field counts obligations newly
blocked during that cycle. Inspect all blocked obligations with:

```bash
argus owner list --team TEAM --status blocked --json
```

See [Persistent Team Ownership](ownership.md) for proof and recovery rules.

`retro` runs team and company learning. Use these commands for manual checks:

```bash
argus retro run
argus retro run --team dev
argus retro run --company-only
argus retro notify --team dev
argus retro summary
```

The watchdog runs `argus ready` and records an error alert if readiness fails.
Backups copy run-root artifacts and, when `pg_dump` and `ARGUS_DB_DSN` are
available, write a Postgres dump.
