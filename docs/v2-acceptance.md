# Acceptance

The acceptance contract is the Python v2 test gate:

```bash
python scripts/gate.py
```

The gate must pass with:

- no skipped or xfailed tests under `ARGUS_GATE=1`
- warnings as errors
- strict pytest markers and config
- coverage at 80% or higher

Phase-completion checks for the pure-v2 migration:

- `argus` console script points to `argus.v2.cli:main`
- no legacy shell or Bats files are tracked
- no retired product tree remains
- dashboard reads v2 Postgres state
- live launchd jobs call `python -m argus.v2.cli` or the installed `argus`
