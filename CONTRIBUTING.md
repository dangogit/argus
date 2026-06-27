# Contributing to Argus

Thanks for your interest. Argus is early. Issues and pull requests are welcome.

## Ground rules

- **Use issues for planned work.** Bugs, docs, onboarding gaps, connectors, and
  engine work should start as a GitHub issue unless the change is a tiny typo or
  release housekeeping.
- **Use pull requests for code changes.** Keep `main` releasable. Branch from
  `main`, use a focused branch name like `fix/slack-reply-loop` or
  `codex/123-onboarding-doctor`, and squash merge after CI passes.
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

## Labels

Use labels to make public work scannable:

- `bug`, `docs`, `onboarding`, `connector`, `engine`, `channel`, `security`
- `good first issue`, `help wanted`
- `priority: high`, `priority: medium`, `priority: low`

Setup questions belong in Discussions. Security reports belong in private
vulnerability reports, never public issues.

## Release cycle

Argus is pre-1.0.

- Patch release: install, runtime, security, or documented behavior fix.
- Minor release: meaningful feature batch or new supported connector/channel.
- Every release updates `CHANGELOG.md`, tags `v0.x.y`, creates a GitHub release,
  and runs the Release workflow. PyPI publishing only happens from a GitHub
  release event.
