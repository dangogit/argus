# Engines

V2 uses the Python engine package in `src/argus/engine`.

Supported engine names in config:

- `echo`
- `scripted`
- `codex`
- `claude-code`
- `hermes`
- `openrouter` (OpenAI-compatible HTTP; needs `OPENROUTER_API_KEY`)
- `ollama` (local OpenAI-compatible HTTP; no key)

Engine config example:

```yaml
company:
  defaults:
    engine: { engine: codex, model: gpt-5.5 }
```

`openrouter` and `ollama` free you from depending on a vendor agent CLI - they
call an OpenAI-compatible `/chat/completions` endpoint over HTTP (stdlib only).
Configure via environment, not YAML:

| Engine | Env | Default |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` (required), `ARGUS_MODEL` or `ARGUS_OPENROUTER_MODEL`, `ARGUS_OPENROUTER_BASE_URL` | model `openai/gpt-4o-mini`, base `https://openrouter.ai/api/v1` |
| `ollama` | `ARGUS_MODEL` or `ARGUS_OLLAMA_MODEL`, `ARGUS_OLLAMA_BASE_URL` | model `llama3.1`, base `http://localhost:11434/v1` |

`ARGUS_ENGINE_HTTP_TIMEOUT` (seconds, default 120) bounds the HTTP call.

Use `argus doctor --live` to verify the configured engine can answer.
