# Live Slack Setup

This runbook connects Argus to Slack for one or more projects. Use it with
Codex, Claude Code, or by hand.

## Agent Prompt

Use this prompt in Codex or Claude Code from the Argus checkout:

```text
Read AGENTS.md and docs/slack-live.md. Create or update ignored local
argus.yaml and .env.local. Keep secrets out of git. Add one Argus team per
project. Use one Slack channel ID per team. Run validate, validate-roles,
doctor, and one inbound Slack smoke before starting live serve.
```

## Required Model

Argus routes Slack inbound by `channel.type` plus `channel_id`.

That means:

- One Slack channel can route to one Argus team.
- Duplicate Slack channel IDs across teams fail `doctor --deep` and
  `go-live`, because inbound routing would be ambiguous.
- For many projects, use one Slack channel per project, or start with one
  initial project.
- Argus receives normal public-channel messages when using the manifest. Use
  dedicated Argus project channels, not busy general-purpose channels.

## 1. Create Local Env

Copy `.env.example` to an ignored env file:

```bash
cp .env.example .env.local
```

Set these values:

```bash
ARGUS_CONFIG=/absolute/path/to/argus.yaml
ARGUS_CONFIG_V2=/absolute/path/to/argus.yaml
ARGUS_DB_DSN="host=127.0.0.1 port=5440 dbname=argus user=argus password=argus"
ARGUS_WEBHOOK_SECRET=replace-with-local-random-secret
SLACK_BOT_TOKEN=xoxb-replace-with-real-token
SLACK_SIGNING_SECRET=replace-with-real-signing-secret
```

`ARGUS_WEBHOOK_SECRET` is for local `argus inbound handle` smoke tests.
Slack live webhooks use `SLACK_SIGNING_SECRET`.

Load env files for every command:

```bash
export ARGUS_ENV_FILES="$PWD/.env.local"
```

## 2. Create Project Config

Start with generated Slack config:

```bash
argus init --config argus.yaml --force --channel slack
```

Then edit `argus.yaml`:

```yaml
company:
  defaults:
    engine: { engine: echo }
    webhook_secret: "${env:ARGUS_WEBHOOK_SECRET}"
teams:
  - name: my-project
    project:
      repo: /absolute/path/to/my-project
      base_branch: main
      work_branch_prefix: argus/my-project
      github_repo: owner/repo
    roles:
      - { name: developer, kind: builder, prompt: "Make the smallest safe change." }
    pipeline: { stages: [developer], max_iters: 1 }
    channels:
      - type: slack
        role: control
        channel_id: C1234567890
        secret_ref: "${env:SLACK_BOT_TOKEN}"
        config:
          signing_secret: "${env:SLACK_SIGNING_SECRET}"
```

Use a real Slack conversation ID for `channel_id`.

## 3. Validate Local Runtime

```bash
argus db migrate
argus validate
argus validate-roles
argus projects list
argus doctor
```

Run a local inbound-only smoke before touching Slack:

```bash
cat > /tmp/slack_payload.json <<'JSON'
{
  "type": "event_callback",
  "event_id": "Ev-local-1",
  "event": {
    "type": "app_mention",
    "user": "U123",
    "channel": "C1234567890",
    "ts": "1782370000.000100",
    "text": "<@B123> status please"
  }
}
JSON

argus inbound handle --channel slack --secret "$ARGUS_WEBHOOK_SECRET" --file /tmp/slack_payload.json
```

Expected:

```text
inbound handle: status=200 ingested=1
```

## 4. Configure Slack App

Fast path:

1. Open `https://api.slack.com/apps`.
2. Click `Create New App`.
3. Choose `From an app manifest`.
4. Select your workspace.
5. Copy `examples/slack-app-manifest.yaml`.
6. Replace `https://YOUR_PUBLIC_ARGUS_URL/slack` with your real tunnel URL.
7. Paste YAML and create the app.
8. Basic Information: copy Signing Secret to `SLACK_SIGNING_SECRET`.
9. Install App: install or reinstall to workspace.
10. OAuth & Permissions: copy Bot User OAuth Token to `SLACK_BOT_TOKEN`.
11. Invite app to the target project channel.

Optional app icon assets live in `docs/assets/argus-slack-icon-1024.png` and
`docs/assets/argus-slack-icon.svg`.

Manual path, if you do not use the manifest:

1. Basic Information: copy Signing Secret to `SLACK_SIGNING_SECRET`.
2. OAuth & Permissions: add bot scopes: `chat:write`, `app_mentions:read`,
   `channels:history`, and `channels:read`.
3. Event Subscriptions: enable events and set Request URL to your `/slack` URL.
4. Subscribe to bot events: `app_mention` and `message.channels`.
5. Reinstall app to workspace after scope or event changes.
6. Invite app to the target channel.

For private Slack channels, also add `groups:history`, `groups:read`, and
`message.groups`.
Keep one dedicated Slack channel per Argus team.

## 5. Expose Local Receiver

Start Argus:

```bash
argus serve --host 127.0.0.1 --port 8787
```

Expose it with a tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

If using ngrok:

```bash
ngrok http 8787
```

Slack Event Subscriptions Request URL:

```text
https://your-public-tunnel.example/slack
```

Slack will send `url_verification`. Argus returns the challenge when
`SLACK_SIGNING_SECRET` matches.

## 6. Live Test

Send a message in the configured project channel:

```text
status please
```

Then check:

```bash
argus status
```

If inbound works and outbound replies should be enabled, run:

```bash
argus up --iterations 1
```

Outbound Slack replies require a real `SLACK_BOT_TOKEN`, `chat:write`, and the
app installed in the target channel.

For live operation, keep the worker loop running continuously:

```bash
argus up --poll 10
```

`go-live` requires at least one inbound Slack event and one sent Slack reply for
each configured Slack team channel. Use `--fresh-slack-proof` when you want to
require proof from the last 30 minutes. It also checks `conversations.history`
so mention-only Slack apps do not look fully operational. If it says
`history missing_scope`, add `channels:history`, reinstall the app, and rerun.
If `info_scope` is advisory, add `channels:read` so Argus can prove channel
names and membership. If it says `no event recorded`, send a fresh message in
that project channel and rerun. If it says `no reply recorded`, run or restart
`argus up --poll 10` and confirm the manager replied.

If Slack mentions arrive but replies only happen after a manual
`argus up --iterations 1`, the install is still a smoke test. Continue with
[Live Onboarding](live-onboarding.md) to add a manager role, always-on workers,
PM auto-fix, and a stable webhook URL.

## Troubleshooting

- `status=401`: wrong `ARGUS_WEBHOOK_SECRET` for local smoke, or wrong
  `SLACK_SIGNING_SECRET` for live Slack.
- `status=200 ingested=0`: channel ID in payload does not match any configured
  team, or owner allowlist rejected the sender.
- `history missing_scope`: Slack app has not been reinstalled with
  `channels:history`; public project-channel messages will not reach Argus.
- Slack URL verification fails: Request URL must end with `/slack`, tunnel must
  reach `argus serve`, and signing secret must match app Basic Information.
- Outbound fails with `slack channel missing bot token`: set `SLACK_BOT_TOKEN`
  and reload env.
