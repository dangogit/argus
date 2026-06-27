import json
from pathlib import Path

from argus.v2.channels.telegram import TelegramChannel

RAW = json.loads((Path(__file__).parent / "fixtures" / "channels" / "telegram_update.json").read_text())


def test_parse_extracts_message_skips_edited():
    msgs = TelegramChannel().parse_inbound(RAW)
    assert len(msgs) == 1  # the edited_message is ignored
    m = msgs[0]
    assert m.chat_id == "-100123" and m.text == "deploy staging"
    assert m.dedup_key == "555" and m.sender == "daniel"


def test_parse_cursor_offset_is_max_update_id():
    _, offset = TelegramChannel.parse_with_offset(RAW)
    assert offset == 557  # max update_id + 1 (Telegram getUpdates convention)
