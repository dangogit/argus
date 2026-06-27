# Support

Argus is pre-1.0 alpha. Public setup questions happen through
[GitHub Discussions](https://github.com/dangogit/argus/discussions). Bugs and
feature requests stay in GitHub issues.

## Ask A Setup Question

Open a [GitHub Discussion](https://github.com/dangogit/argus/discussions) and
include:

- OS and install path (`pipx`, `uv`, or source checkout).
- `argus --version`.
- Selected onboarding mode (`chat-only`, `monitor-only`, or `pm-propose-pr`).
- Redacted `argus doctor --deep --json` output.
- Whether `argus go-live` reports `operational`, `configured-only`, or
  `blocked`.

Do not paste tokens, phone numbers, private repo names, customer data, Slack
payloads with user data, or unredacted env files.

## Report A Bug

Use the bug report template. Include exact commands, expected behavior, actual
behavior, and redacted logs.

## Request A Feature

Use the feature request template. For connectors or channels, include provider
name, required credentials, webhook shape, and expected dry-run behavior.

## Security Reports

Do not open public issues for vulnerabilities. Use the repository Security tab
and read [SECURITY.md](SECURITY.md).
