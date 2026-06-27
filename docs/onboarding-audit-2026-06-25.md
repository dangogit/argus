# Argus onboarding audit - 2026-06-25

Scope: fresh clone from `https://github.com/dangogit/argus.git` at `8e69158`.
Flow tested as new source-checkout user. WhatsApp skipped. Telegram and Slack
paths tested instead.

Environment:

- macOS local machine
- `python3`: `Python 3.9.6`
- `/opt/homebrew/bin/python3.12`: `Python 3.12.13`
- Docker `29.4.3`
- `gh` `2.93.0`
- Node `v26.0.0`

## What worked

- `git clone https://github.com/dangogit/argus.git` worked.
- Editable install worked with Python 3.12:
  `python -m pip install -e '.[dev]'`.
- `argus version` printed `argus 0.1.0`.
- `argus init --config argus.yaml --force` wrote `argus.yaml`.
- README Postgres command worked after first Docker image pull:
  `pgvector/pgvector:pg17`.
- `argus db migrate` applied 17 migrations.
- `argus validate`, `argus validate-roles`, and `argus doctor` passed.
- Echo loop worked:
  `argus submit --team demo ...`, `argus up --iterations 1`, `argus status`.
- Non-editable install from repo also worked:
  `python -m pip install /tmp/argus-onboarding-VPb9wr/argus`.
- Wheel-installed CLI worked outside source tree. `argus db migrate` reported
  `applied=0`, and `argus doctor` saw `db migrations=17`.
- Telegram inbound can work locally after adding config:
  `company.defaults.webhook_secret`, a Telegram channel binding, correct
  `${env:TELEGRAM_BOT_TOKEN}` secret ref, and `--secret local-secret`.
  `argus wa handle --channel telegram --secret local-secret --file telegram_payload.json`
  returned `status=200 ingested=1`.

## Bad onboarding experience

1. README says `python3 -m venv .venv`, but this Mac's `python3` is 3.9.6.
   Project requires Python 3.11+. New macOS users can hit this immediately.
   Docs should say `python3.11` or `python3.12`, or offer a `uv` path.

2. `argus --version` fails with argparse output:
   `argus: error: the following arguments are required: cmd`.
   CLI has `argus version`, but many users will try `--version`.

3. `argus init` creates a minimal config with no channel and no
   `company.defaults.webhook_secret`. The inbound docs then fail with 401 unless
   user knows to add webhook secret and pass matching `--secret`.

4. Telegram config is not documented. I had to infer channel shape from schema
   and code:
   `channels: [{ type: telegram, role: control, channel_id: "12345", secret_ref: "${env:TELEGRAM_BOT_TOKEN}" }]`.

5. Secret ref syntax is easy to get wrong. Bare `TELEGRAM_BOT_TOKEN` caused a
   Python traceback:
   `argus.v2.config.loader.ConfigError: bad secret ref: 'TELEGRAM_BOT_TOKEN'`.
   CLI should catch config errors and print one clean message.

6. Generic inbound command is named `argus wa handle`, even for Telegram and
   Slack. That reads as WhatsApp-only.

7. Telegram local smoke with dummy token ingested inbound successfully, then
   `argus up --iterations 1` tried outbound Telegram send and failed with
   `404 Not Found` from `https://api.telegram.org/botdummy-token/sendMessage`.
   Need docs to say first Telegram smoke requires real bot token, or provide a
   dry-run / fake-reply mode.

8. Slack is worse than Telegram. Schema accepts `type: slack`, `argus validate`
   passes, `argus doctor --live` passes, and `argus ready --live` passes. But
   `argus wa handle --channel slack ...` returns `status=400 ingested=0`
   because no Slack adapter is registered.

9. `doctor --live` and `ready --live` do not catch unsupported channel adapters.
   They should fail if config names channel types not present in channel
   registry.

10. Quickstart expected status says rows in `events`, `requests`, `jobs`, and
    `actions` where applicable. Actual echo loop showed:
    `events: processed=1`, `requests: none`, `jobs: none`, `actions: done=1`.
    Maybe correct, but docs make it feel like missing rows are wrong.

11. First Docker pull took about 30 seconds. README does not mention waiting for
    Postgres readiness. It passed here, but likely flaky on slower machines.

## Fix queue

- Add Python 3.11+ install path for macOS, preferably `uv` plus `python3.12`
  fallback.
- Add `argus --version`.
- Add `argus init --channel telegram` or documented Telegram config block.
- Rename or alias `argus wa handle` to `argus inbound handle`.
- Catch `ConfigError` in CLI and print clean user-facing errors.
- Add `TELEGRAM_BOT_TOKEN` and webhook secret examples to `.env.example`.
- Add Telegram smoke doc with sample payload and real-token warning.
- Either implement Slack adapter or remove `slack` from accepted channel types.
- Extend doctor/ready to validate configured channel types against registry.
- Adjust quickstart status wording to match actual echo flow.
