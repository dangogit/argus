# Engines

V2 uses the Python engine package in `src/argus/engine`.

Supported engine names in config:

- `echo`
- `scripted`
- `codex`
- `claude-code`
- `hermes`

Engine config example:

```yaml
company:
  defaults:
    engine: { engine: codex, model: gpt-5.5 }
```

Use `argus doctor --live` to verify the configured engine can answer.
