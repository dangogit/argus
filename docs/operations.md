# Operations

Health checks:

```bash
argus doctor
argus ready
argus status
argus verify
```

Host jobs:

```bash
argus launchd render --out /tmp/argus-launchd --env-file /path/to/runtime.env
argus host render --os linux --jobs-dir ./jobs --out /tmp/argus-systemd
argus host status
argus host watchdog
argus host backup
argus host logrotate
```

`argus launchd render` emits the built-in always-on runtime jobs:

- `serve`
- `up`
- `poll`
- `retro` every 86400 seconds
- `watchdog`
- `backup`
- `logrotate`

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
