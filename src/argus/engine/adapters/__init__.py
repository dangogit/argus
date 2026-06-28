"""Adapter registry. An adapter is a callable (prompt: str) -> EngineResult
that raises EngineOutageError when its CLI is missing or hard-fails."""

from argus.engine.adapters import claude_code, codex, echo, hermes, openai_compat

ADAPTERS = {
    "echo": echo.run,
    "claude-code": claude_code.run,
    "codex": codex.run,
    "hermes": hermes.run,
    "openrouter": openai_compat.openrouter,
    "ollama": openai_compat.ollama,
}
