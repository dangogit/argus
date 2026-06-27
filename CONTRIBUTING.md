# Contributing to Argus

Thanks for your interest. Argus is early. Issues and pull requests are welcome.

## Ground rules

- **`argus verify` must stay green from a source checkout.** It runs the Python
  v2 gate with strict pytest settings, warnings as errors, and the coverage
  floor. Installed wheels do not ship the source test suite.
- **Every change ships with tests.** New backend behavior gets focused pytest
  coverage under `tests/python/v2`. Dashboard behavior gets a Vitest test.
  The `echo` engine and temp dirs keep tests hermetic: no live model, no
  network, no secrets.
- **No em-dashes** anywhere (code, comments, docs, commit messages). Use commas,
  colons, or parentheses.
- **Never commit `node_modules`, `.next`, or anything under `run/`** (all
  gitignored).

## Local setup

```bash
git clone <repo-url> ~/argus && cd ~/argus
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
argus verify   # source checkout only

# optional local runtime smoke database
docker compose up -d postgres

# dashboard
cd dashboard && npm install && npm run test && npm run build
```

## Where things live

See [docs/architecture.md](docs/architecture.md) for the layer map. Most work
lands under `src/argus/v2/`, `tests/python/v2/`, `dashboard/`, or `docs/`.

## Scope of changes

Keep pull requests focused on one subsystem. If you are adding a connector,
notifier, engine adapter, or scheduled job, prefer the extension points
described in the architecture doc so the change composes cleanly.
