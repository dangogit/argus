# Inbound Channels

The v2 receiver accepts webhook payloads and routes them through configured
channel bindings.

```bash
argus serve --host 127.0.0.1 --port 8787
argus inbound handle --channel telegram --secret "$ARGUS_WEBHOOK_SECRET" --file payload.json
```

Supported channel adapters include `whatsapp`, `telegram`, `slack`, `fake`,
and `cli`. `argus wa handle` still exists as a backwards-compatible alias for
WhatsApp-era scripts. Prefer `argus inbound handle` for new scripts.

## Telegram Smoke

Create a local config with Telegram enabled:

```bash
argus init --config argus.yaml --force --channel telegram
export ARGUS_CONFIG="$PWD/argus.yaml"
export ARGUS_CONFIG_V2="$PWD/argus.yaml"
export ARGUS_WEBHOOK_SECRET=local-secret
export TELEGRAM_BOT_TOKEN=123456:replace-with-real-token
```

`argus init --channel telegram` writes this channel binding:

```yaml
company:
  defaults:
    webhook_secret: "${env:ARGUS_WEBHOOK_SECRET}"
teams:
  - name: demo
    channels:
      - type: telegram
        role: control
        channel_id: "12345"
        secret_ref: "${env:TELEGRAM_BOT_TOKEN}"
```

For a local inbound-only smoke, set `channel_id` to the test chat ID and pass a
saved Telegram `getUpdates` payload:

```json
{
  "ok": true,
  "result": [
    {
      "update_id": 557,
      "message": {
        "message_id": 42,
        "from": { "id": 9001, "username": "maintainer" },
        "chat": { "id": 12345, "type": "private" },
        "date": 1782370000,
        "text": "status please"
      }
    }
  ]
}
```

```bash
argus inbound handle --channel telegram --secret "$ARGUS_WEBHOOK_SECRET" --file telegram_payload.json
```

Inbound parsing does not call Telegram. Outbound replies do call
`sendMessage`, so `argus up` needs a real bot token and chat ID.

## Slack Smoke

Create a local config with Slack enabled:

```bash
argus init --config argus.yaml --force --channel slack
export ARGUS_CONFIG="$PWD/argus.yaml"
export ARGUS_CONFIG_V2="$PWD/argus.yaml"
export ARGUS_WEBHOOK_SECRET=local-secret
export SLACK_BOT_TOKEN=xoxb-replace-with-real-token
export SLACK_SIGNING_SECRET=replace-with-real-signing-secret
```

`argus init --channel slack` writes this channel binding:

```yaml
company:
  defaults:
    webhook_secret: "${env:ARGUS_WEBHOOK_SECRET}"
teams:
  - name: demo
    channels:
      - type: slack
        role: control
        channel_id: "C1234567890"
        secret_ref: "${env:SLACK_BOT_TOKEN}"
        config:
          signing_secret: "${env:SLACK_SIGNING_SECRET}"
```

Set `channel_id` to the Slack conversation ID. For the documented project
channel flow, subscribe the Slack app to `app_mention` and `message.channels`,
add bot scopes `chat:write`, `app_mentions:read`, `channels:history`, and
`channels:read`, then invite the app to that channel. Use one dedicated Slack
channel per Argus team. For private channels, use `groups:history`,
`groups:read`, and `message.groups` too.

For real Slack Event Subscriptions, expose Argus and use the `/slack` path:

```bash
argus serve --host 127.0.0.1 --port 8787
```

Request URL:

```text
https://your-public-tunnel.example/slack
```

Slack signs those HTTP requests with `X-Slack-Signature` and
`X-Slack-Request-Timestamp`; Argus verifies them with
`SLACK_SIGNING_SECRET`. URL verification is handled by returning Slack's
challenge value. `ARGUS_WEBHOOK_SECRET` is still useful for local
`argus inbound handle` smoke tests.

For a local inbound-only smoke, save this payload after changing `channel` to
your configured channel ID:

```json
{
  "type": "event_callback",
  "event_id": "Ev123",
  "event": {
    "type": "app_mention",
    "user": "U123",
    "channel": "C1234567890",
    "ts": "1782370000.000100",
    "text": "<@B123> status please"
  }
}
```

```bash
argus inbound handle --channel slack --secret "$ARGUS_WEBHOOK_SECRET" --file slack_payload.json
```

Inbound parsing does not call Slack. Outbound replies do call
`chat.postMessage`, so `argus up` needs a real bot token and the app must be in
the target conversation.

WhatsApp uses Evolution API payloads, owner allowlists, optional voice-note
transcription, chunked outbound messages, and best-effort composing presence.

Voice transcription is opt-in:

```bash
export ARGUS_WA_VOICE=1
export ARGUS_WA_TRANSCRIBE_CMD="/path/to/transcriber"
```

Without `ARGUS_WA_TRANSCRIBE_CMD`, the local whisper path requires
`ARGUS_WHISPER_MODEL`, `whisper-cli`, and `ffmpeg`.
