# Updating

Argus can be installed from GitHub with `pipx`, from GitHub with `uv`, or from a
source checkout. Match the update command to the install path you used.

## Check Current Version

```bash
argus --version
argus doctor
```

If Argus is configured for live operation, also check:

```bash
argus ready --live
argus doctor --deep --json
```

## Update A `pipx` Install

For the recommended GitHub installer path:

```bash
pipx upgrade argus-agent
argus --version
```

If `pipx upgrade` cannot resolve the GitHub spec, reinstall from the public
repository:

```bash
pipx install --force --python python3.12 "git+https://github.com/dangogit/argus.git"
argus --version
```

## Update A `uv` Tool Install

```bash
uv tool upgrade argus-agent
argus --version
```

If needed, reinstall from GitHub:

```bash
uv tool install --force --python 3.12 "git+https://github.com/dangogit/argus.git"
argus --version
```

## Update A Source Checkout

```bash
git pull --ff-only
. .venv/bin/activate
python -m pip install -e '.[dev]'
argus --version
python scripts/gate.py
```

Apply database migrations after updating code:

```bash
argus db migrate
argus validate
argus validate-roles
argus doctor --deep
```

## Update Always-On Services

After package or source update, restart the process manager that runs Argus.

For launchd-rendered jobs, rerender if command paths, env paths, or labels
changed:

```bash
argus launchd render --out /tmp/argus-launchd --env-file /path/to/runtime.env
```

Then reload with your operator runbook. Confirm:

```bash
argus ready --live
argus go-live --mode chat-only --public-url https://argus.example.com/slack
```

For `monitor-only` or `pm-propose-pr`, use the same `go-live` mode and required
connector flags as your deployment.

## Roll Back

For source installs:

```bash
git checkout <known-good-commit>
. .venv/bin/activate
python -m pip install -e '.[dev]'
argus db migrate
argus doctor --deep
```

Database migrations are forward-only. Restore from backup if a rollback needs a
previous schema.

## Uninstall

For `pipx`:

```bash
pipx uninstall argus-agent
```

For `uv`:

```bash
uv tool uninstall argus-agent
```

Argus does not delete your private config, env files, Postgres database,
launchd/systemd units, logs, or run root. Remove those manually only after
backing up anything you need.

## Before Reporting Update Bugs

Include:

- OS and install path (`pipx`, `uv`, or source checkout).
- `argus --version`.
- Redacted `argus doctor --deep --json`.
- Whether migrations were run.
- Process manager status when the issue affects live workers.
