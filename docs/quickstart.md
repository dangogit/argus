# Quickstart

This path runs the Python Argus core locally with Postgres, the `echo` engine,
and one demo team. It proves the runtime loop before any paid model, optional
Next.js dashboard, or live connector is configured.

## 1. Install

```bash
python3.12 -m venv .venv  # or any Python 3.11+
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

On macOS, `/usr/bin/python3` can be older than 3.11. Use Homebrew Python,
`python3.12`, `python3.11`, or another Python 3.11+ interpreter.
On Windows, install Python 3.11+ and Git for Windows, then run
`scripts/install.ps1` from PowerShell or use the manual `pipx` install command
from the README.

## 2. Start Postgres

```bash
docker compose up -d postgres
until docker compose exec -T postgres pg_isready -U argus -d argus; do sleep 1; done
```

This uses the repository `docker-compose.yml` and leaves data in a named Docker
volume. Reset local smoke state with `docker compose down -v`.

If your Docker install does not support Compose, use the direct container
fallback:

```bash
docker run -d --rm --name argus-postgres \
  -e POSTGRES_USER=argus \
  -e POSTGRES_PASSWORD=argus \
  -e POSTGRES_DB=argus \
  -p 5440:5432 \
  pgvector/pgvector:pg17

until docker exec argus-postgres pg_isready -U argus -d argus; do sleep 1; done
```

## 3. Create Local Config

```bash
argus init --config argus.yaml --force
export ARGUS_CONFIG="$PWD/argus.yaml"
export ARGUS_CONFIG_V2="$PWD/argus.yaml"
export ARGUS_DB_DSN="host=127.0.0.1 port=5440 dbname=argus user=argus password=argus"
export ARGUS_RUN_ROOT="$PWD/run"
```

`argus init` uses the `echo` engine, so this quickstart does not need API keys.
`argus.yaml` is local runtime config and is ignored by git.

For real credentials later, keep them in `.env` or another operator-owned env
file and export `ARGUS_ENV_FILES=/absolute/path/to/file`. Do not put tokens,
database passwords, webhook secrets, or Apps Script keys in YAML. Start from
`.env.example` when moving beyond this local smoke test.

## 4. Migrate And Check

```bash
argus db migrate
argus validate
argus validate-roles
argus doctor
```

## 5. Run One Agent Loop

```bash
argus submit --team demo "Review this repo and propose one small improvement"
argus up --iterations 1
argus status
```

Expected result: `status` shows processed `events` and a completed `action`.
`requests` and `jobs` may be `none` in the echo quickstart. The first loop is
intentionally local and propose-only.

## 6. Add Real Engines Later

Edit `argus.yaml` only after the local loop works:

```yaml
company:
  defaults:
    engine:
      engine: codex
```

Then run:

```bash
argus doctor --live
```

From a source checkout, `argus verify` also runs the full Python gate.
Keep `echo` as the first smoke test. It separates runtime problems from model
or credential problems.

Before adding support inboxes, polling connectors, or always-on host jobs, read
[Configuration](configuration.md). It explains what belongs in
`company.defaults`, what belongs in `teams[].project`, and what must stay in
env files.
