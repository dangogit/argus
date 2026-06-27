# Argus Installer Skills

This directory contains agent-facing skills for Codex, Claude Code, and other
coding agents installing Argus for a user.

- [argus-live-onboarding](argus-live-onboarding/SKILL.md): inspect the local
  computer, ask only for missing decisions or secret locations, generate private
  config, and prove the install with `doctor --deep` and `go-live`.

These are not runtime prompt skills for Argus worker roles. Runtime skills live
under `prompts/skills/`.
