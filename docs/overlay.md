# Private Runtime Data

The v2 product does not require a tracked overlay tree. Keep private data in
untracked config, env files, and runtime directories.

Recommended live files:

- `argus.yaml` for typed v2 config
- an env file referenced by `ARGUS_ENV_FILES`
- a Postgres database referenced by `ARGUS_DB_DSN`
- `ARGUS_RUN_ROOT` for artifacts such as media and generated content

Use `argus config convert` to migrate older flat config files into v2 YAML.
