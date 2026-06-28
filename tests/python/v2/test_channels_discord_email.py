"""Discord parse + email message build + capability-optional registration."""
from __future__ import annotations

import argus.v2.channels  # noqa: F401  (registers adapters)
from argus.v2.channels.base import REGISTRY
from argus.v2.channels.discord import DiscordChannel
from argus.v2.channels.email import build_message


def test_both_channels_registered():
    assert "discord" in REGISTRY and "email" in REGISTRY


def test_discord_parses_messages_and_cursor():
    raw = [  # Discord returns newest-first
        {"id": "30", "channel_id": "c1", "content": "later", "author": {"username": "alice"}},
        {"id": "20", "channel_id": "c1", "content": "earlier", "author": {"username": "bob"}},
    ]
    msgs, offset = DiscordChannel.parse_with_offset(raw)
    assert [m.text for m in msgs] == ["earlier", "later"]  # reversed to chronological
    assert msgs[0].chat_id == "c1" and msgs[0].dedup_key == "20"
    assert offset == "30"  # highest snowflake


def test_discord_skips_bots_and_empty():
    raw = [
        {"id": "5", "channel_id": "c", "content": "hi", "author": {"username": "me", "bot": True}},
        {"id": "6", "channel_id": "c", "content": "", "author": {"username": "u"}},
        {"id": "7", "channel_id": "c", "content": "real", "author": {"username": "u"}},
    ]
    msgs, offset = DiscordChannel.parse_with_offset(raw)
    assert [m.text for m in msgs] == ["real"]
    assert offset == "7"  # cursor still advances past skipped messages


def test_discord_empty_list():
    msgs, offset = DiscordChannel.parse_with_offset([])
    assert msgs == [] and offset is None


def test_email_parse_inbound_is_empty():
    # Inbound email is the email_imap connector's job; the channel only sends.
    from argus.v2.channels.email import EmailChannel
    assert EmailChannel().parse_inbound({}) == []


def test_email_build_message():
    msg = build_message("ops@example.com", "deploy failed", from_addr="argus@example.com",
                        subject="Alert")
    assert msg["To"] == "ops@example.com"
    assert msg["From"] == "argus@example.com"
    assert msg["Subject"] == "Alert"
    assert "deploy failed" in msg.get_content()
