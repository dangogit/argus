import json
import sys
from pathlib import Path

from argus.v2.channels.whatsapp import WhatsAppChannel

RAW = json.loads((Path(__file__).parent / "fixtures" / "channels" / "evolution_upsert.json").read_text())


def test_parse_extracts_chat_text_and_id():
    msgs = WhatsAppChannel().parse_inbound(RAW)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.chat_id == "120363@g.us" and m.text == "fix the login bug"
    assert m.dedup_key == "WAMSG1" and m.sender == "Daniel"


def test_parse_extracts_group_sender_ref():
    raw = json.loads(json.dumps(RAW))
    raw["data"]["key"]["participant"] = "owner@lid"
    msgs = WhatsAppChannel().parse_inbound(raw)
    assert msgs[0].sender_ref == "owner@lid"


def test_parse_ignores_from_me_and_non_message():
    raw = {"event": "messages.upsert",
           "data": {"key": {"remoteJid": "x", "id": "1", "fromMe": True},
                    "message": {"conversation": "echo"}}}
    assert WhatsAppChannel().parse_inbound(raw) == []
    assert WhatsAppChannel().parse_inbound({"event": "presence.update"}) == []


def test_parse_audio_message_marks_audio_media():
    raw = json.loads(json.dumps(RAW))
    raw["data"]["message"] = {"audioMessage": {"mimetype": "audio/ogg", "seconds": 4, "ptt": True}}

    msg = WhatsAppChannel().parse_inbound(raw)[0]

    assert msg.text == ""
    assert msg.media == [{
        "kind": "audio",
        "mime": "audio/ogg",
        "message_id": "WAMSG1",
        "ptt": True,
        "seconds": 4,
    }]


def test_send_chunks_text_and_posts_presence(monkeypatch):
    calls = []

    class Response:
        def __init__(self, key):
            self.key = key

        def raise_for_status(self):
            return None

        def json(self):
            return {"key": {"id": self.key}}

    class Httpx:
        @staticmethod
        def post(url, headers, json, timeout):
            calls.append((url, headers, json, timeout))
            return Response(f"id-{len(calls)}")

    class Binding:
        channel_id = "120@g.us"
        secret = "key"
        config = {
            "base_url": "http://evolution",
            "instance": "argus",
            "max_chars": 5,
            "max_parts": 3,
            "presence": True,
        }

    monkeypatch.setitem(sys.modules, "httpx", Httpx)

    msg_id = WhatsAppChannel().send(Binding(), "hello world")

    assert msg_id == "id-2"
    assert calls[0][0] == "http://evolution/chat/sendPresence/argus"
    assert calls[0][2]["presence"] == "composing"
    assert [call[2]["text"] for call in calls[1:]] == ["hello", " worl", "d"]
