# Roadmap

Argus is a self-hosted company of AI agents for software projects. The roadmap
stays focused on operational usefulness, safe defaults, and public install
quality.

## Now

- Keep public install green: source install, wheel smoke, Docker smoke, and
  public launch checker.
- Improve live onboarding: Slack, Telegram, local engine detection, and go-live
  checks.
- Harden public repo operations: issue templates, labels, CI, branch protection,
  release workflow, and security reporting.
- Document the operational path for Codex and Claude Code users.

## Next

- MCP client support for connector and tool access.
- MCP server support exposing Argus status, alerts, proposed PRs, and lessons.
- Provider breadth: OpenRouter and Ollama paths for users who do not want to
  depend on one agent CLI.
- Linux runtime parity for the opinionated always-on bundle.
- Connector hardening: dry-run output, backoff, clearer missing-secret states.

## Later

- agentskills.io compatibility for portable skills.
- Additional ops channels where they make sense, starting with Discord and a
  generic email gateway.
- Better public examples for monitor-only and pm-propose-pr modes.
- Hosted docs site if README and docs directory become hard to navigate.

## Non-goals

- No hosted Argus cloud holding user secrets.
- No auto-merge or auto-deploy by default.
- No general personal assistant scope.
- No smart home, voice assistant, or social media automation surface.
