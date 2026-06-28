"""Slack Events API adapter and chat.postMessage sender."""
from __future__ import annotations

from argus.v2.channels.base import InboundMessage, register


@register
class SlackChannel:
    type = "slack"

    def parse_inbound(self, raw, secret=None):
        if not isinstance(raw, dict):
            return []
        event = raw.get("event") or {}
        if raw.get("type") != "event_callback" or not isinstance(event, dict):
            return []

        event_type = event.get("type")
        if event_type not in {"message", "app_mention"}:
            return []
        if event_type == "message" and event.get("subtype"):
            return []
        if event.get("bot_id"):
            return []

        channel = event.get("channel")
        text = (event.get("text") or "").strip()
        dedup_key = event.get("ts") or event.get("client_msg_id") or raw.get("event_id")
        if not channel or not text or not dedup_key:
            return []

        sender = str(event.get("user") or event.get("bot_id") or "")
        metadata = {
            "slack_event_type": event_type,
            "slack_ts": event.get("ts"),
            "slack_thread_ts": event.get("thread_ts") or event.get("ts"),
        }
        return [InboundMessage(
            chat_id=str(channel),
            text=text,
            dedup_key=str(dedup_key),
            sender=sender,
            sender_ref=sender,
            metadata={k: v for k, v in metadata.items() if v},
        )]

    def send(self, binding, text: str) -> str:  # pragma: no cover
        import httpx

        if not binding.secret:
            raise RuntimeError("slack channel missing bot token")
        r = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {binding.secret}"},
            json={"channel": binding.channel_id, "text": text},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack chat.postMessage failed: {data.get('error', 'unknown_error')}")
        return str(data.get("ts", ""))

    def update(self, binding, message_id: str, text: str) -> str:  # pragma: no cover
        import httpx

        if not binding.secret:
            raise RuntimeError("slack channel missing bot token")
        r = httpx.post(
            "https://slack.com/api/chat.update",
            headers={"Authorization": f"Bearer {binding.secret}"},
            json={"channel": binding.channel_id, "ts": message_id, "text": text},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack chat.update failed: {data.get('error', 'unknown_error')}")
        return str(data.get("ts", message_id))

    def set_typing(self, binding, thread_ts: str, status: str,
                   loading_messages=None) -> bool:  # pragma: no cover
        """Native Slack AI typing indicator via assistant.threads.setStatus - the
        grey italic 'is thinking...' dots in the thread, optionally cycling
        through loading_messages. Best-effort: returns False instead of raising if
        the token lacks the surface or the call is rejected, so a typing hint never
        breaks the durable status line. status='' clears the indicator. Works with
        the chat:write scope (no assistant-app manifest required).

        ponytail: per-stage fire; Slack auto-clears the indicator on the bot's next
        reply and after a ~2-min idle timeout, which the next stage re-firing
        covers. No local clear timer needed."""
        if not binding.secret or not thread_ts:
            return False
        body = {
            "channel_id": binding.channel_id,
            "thread_ts": thread_ts,
            "status": status or "",
        }
        if loading_messages:
            body["loading_messages"] = list(loading_messages)[:10]
        try:
            import httpx

            r = httpx.post(
                "https://slack.com/api/assistant.threads.setStatus",
                headers={"Authorization": f"Bearer {binding.secret}"},
                json=body,
                timeout=10,
            )
            return bool(r.json().get("ok"))
        except Exception:
            return False
