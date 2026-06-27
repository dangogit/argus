import hashlib
import hmac
import json
from pathlib import Path
import time

import pytest

from argus.v2.channels import receiver
from argus.v2.config import loader


def _cfg(tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo }, webhook_secret: s3cret }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'chatA' } ]\n")
    return loader.load(y)


def test_webhook_ingests_message(conn, tmp_path):
    cfg = _cfg(tmp_path)
    body = json.dumps({"chat_id": "chatA", "id": "m1", "text": "fix login"}).encode()
    status, n = receiver.handle_webhook(conn, cfg, "fake", body, {"x-argus-secret": "s3cret"})
    conn.commit()
    assert status == 200 and n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE source='fake:chatA'")
        assert cur.fetchone()[0] == 1


def test_webhook_accepts_x_argus_token(conn, tmp_path):
    # Evolution posts the shared secret under the x-argus-token header.
    cfg = _cfg(tmp_path)
    body = json.dumps({"chat_id": "chatA", "id": "m1", "text": "fix login"}).encode()
    status, n = receiver.handle_webhook(conn, cfg, "fake", body, {"x-argus-token": "s3cret"})
    conn.commit()
    assert status == 200 and n == 1


def test_webhook_header_lookup_is_case_insensitive(conn, tmp_path):
    # serve() passes a plain dict; Evolution title-cases the header name.
    cfg = _cfg(tmp_path)
    body = json.dumps({"chat_id": "chatA", "id": "m1", "text": "x"}).encode()
    status, n = receiver.handle_webhook(conn, cfg, "fake", body, {"X-Argus-Token": "s3cret"})
    conn.commit()
    assert status == 200 and n == 1


def test_webhook_rejects_bad_secret(conn, tmp_path):
    cfg = _cfg(tmp_path)
    body = json.dumps({"chat_id": "chatA", "id": "m1", "text": "x"}).encode()
    status, n = receiver.handle_webhook(conn, cfg, "fake", body, {"x-argus-secret": "wrong"})
    assert status == 401 and n == 0


def test_webhook_rejects_missing_configured_secret(conn, tmp_path):
    y = tmp_path / "a.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo } }\n"
        "teams:\n  - name: dev\n    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels: [ { type: fake, role: control, channel_id: 'chatA' } ]\n")
    cfg = loader.load(y)
    body = json.dumps({"chat_id": "chatA", "id": "m1", "text": "x"}).encode()
    status, n = receiver.handle_webhook(conn, cfg, "fake", body, {})
    assert status == 401 and n == 0


def test_webhook_unbound_chat_ingests_nothing(conn, tmp_path):
    cfg = _cfg(tmp_path)
    body = json.dumps({"chat_id": "other", "id": "m1", "text": "x"}).encode()
    status, n = receiver.handle_webhook(conn, cfg, "fake", body, {"x-argus-secret": "s3cret"})
    assert status == 200 and n == 0


def test_webhook_rejects_unknown_channel(conn, tmp_path):
    cfg = _cfg(tmp_path)
    status, n = receiver.handle_webhook(conn, cfg, "", b"{}", {"x-argus-secret": "s3cret"})
    assert status == 400 and n == 0


def test_telegram_webhook_ingests_bound_chat(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    y = tmp_path / "tg.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo }, webhook_secret: s3cret }\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels:\n"
        "      - type: telegram\n"
        "        role: control\n"
        "        channel_id: '12345'\n"
        "        secret_ref: '${env:TELEGRAM_BOT_TOKEN}'\n",
        encoding="utf-8",
    )
    cfg = loader.load(y)
    body = json.dumps({
        "ok": True,
        "result": [{
            "update_id": 557,
            "message": {
                "message_id": 42,
                "from": {"id": 9001, "username": "maintainer"},
                "chat": {"id": 12345, "type": "private"},
                "date": 1782370000,
                "text": "status please",
            },
        }],
    }).encode()

    status, n = receiver.handle_webhook(conn, cfg, "telegram", body,
                                        {"x-argus-secret": "s3cret"})
    conn.commit()

    assert status == 200 and n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, source, payload->>'text' FROM events WHERE source='telegram:12345'")
        assert cur.fetchone() == ("dev", "telegram:12345", "status please")


def _slack_headers(body: bytes, signing_secret: str, *, timestamp: int | None = None) -> dict:
    timestamp = str(timestamp or int(time.time()))
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    signature = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }


def _slack_cfg(tmp_path, monkeypatch, *, webhook_secret: bool = False):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signing-test")
    defaults = "defaults: { engine: { engine: echo } }"
    if webhook_secret:
        defaults = "defaults: { engine: { engine: echo }, webhook_secret: s3cret }"
    y = tmp_path / "slack.yaml"
    y.write_text(
        "company:\n"
        "  name: c\n"
        f"  {defaults}\n"
        "teams:\n"
        "  - name: dev\n"
        "    roles: [ { name: developer, kind: builder, prompt: p } ]\n"
        "    pipeline: { stages: [developer] }\n"
        "    channels:\n"
        "      - type: slack\n"
        "        role: control\n"
        "        channel_id: 'C123'\n"
        "        secret_ref: '${env:SLACK_BOT_TOKEN}'\n"
        "        config:\n"
        "          signing_secret: '${env:SLACK_SIGNING_SECRET}'\n",
        encoding="utf-8",
    )
    return loader.load(y)


def test_slack_webhook_ingests_bound_channel_with_signature(conn, tmp_path, monkeypatch):
    cfg = _slack_cfg(tmp_path, monkeypatch)
    body = json.dumps({
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "app_mention",
            "user": "U123",
            "channel": "C123",
            "ts": "1782370000.000100",
            "text": "<@B123> status please",
        },
    }).encode()

    status, n = receiver.handle_webhook(
        conn,
        cfg,
        "slack",
        body,
        _slack_headers(body, "signing-test"),
    )
    conn.commit()

    assert status == 200 and n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT team_id, source, payload->>'text' FROM events WHERE source='slack:C123'")
        assert cur.fetchone() == ("dev", "slack:C123", "<@B123> status please")


def test_slack_webhook_accepts_shared_secret_for_local_handle(conn, tmp_path, monkeypatch):
    cfg = _slack_cfg(tmp_path, monkeypatch, webhook_secret=True)
    body = json.dumps({
        "type": "event_callback",
        "event_id": "Ev124",
        "event": {
            "type": "message",
            "user": "U123",
            "channel": "C123",
            "ts": "1782370000.000200",
            "text": "status please",
        },
    }).encode()

    status, n = receiver.handle_webhook(conn, cfg, "slack", body, {"x-argus-secret": "s3cret"})

    assert status == 200 and n == 1


def test_slack_url_verification_returns_challenge(conn, tmp_path, monkeypatch):
    cfg = _slack_cfg(tmp_path, monkeypatch)
    body = json.dumps({"type": "url_verification", "challenge": "challenge-value"}).encode()

    res = receiver.response_for_webhook(
        conn,
        cfg,
        "slack",
        body,
        _slack_headers(body, "signing-test"),
    )

    assert res.status == 200
    assert res.count == 0
    assert res.body == b"challenge-value"


def test_slack_webhook_rejects_stale_signature(conn, tmp_path, monkeypatch):
    cfg = _slack_cfg(tmp_path, monkeypatch)
    body = json.dumps({"type": "url_verification", "challenge": "challenge-value"}).encode()

    status, n = receiver.handle_webhook(
        conn,
        cfg,
        "slack",
        body,
        _slack_headers(body, "signing-test", timestamp=int(time.time()) - 301),
    )

    assert status == 401 and n == 0


def test_whatsapp_owner_gate_rejects_non_owner(conn, tmp_path):
    y = tmp_path / "wa.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo }, webhook_secret: s3cret }\n"
        "teams:\n  - name: personal\n"
        "    roles: [ { name: manager, kind: front, prompt: p, engine: { engine: echo } } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: whatsapp\n"
        "        role: control\n"
        "        channel_id: '120363@g.us'\n"
        "        config: { owner_ids: ['owner@lid'] }\n")
    cfg = loader.load(y)
    body = json.dumps({
        "event": "messages.upsert",
        "data": {
            "key": {
                "remoteJid": "120363@g.us",
                "id": "m1",
                "fromMe": False,
                "participant": "other@lid",
            },
            "message": {"conversation": "status"},
        },
    }).encode()
    status, n = receiver.handle_webhook(conn, cfg, "whatsapp", body,
                                        {"x-argus-secret": "s3cret"})
    assert status == 200 and n == 0


def test_whatsapp_owner_gate_accepts_owner(conn, tmp_path):
    y = tmp_path / "wa.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo }, webhook_secret: s3cret }\n"
        "teams:\n  - name: personal\n"
        "    roles: [ { name: manager, kind: front, prompt: p, engine: { engine: echo } } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: whatsapp\n"
        "        role: control\n"
        "        channel_id: '120363@g.us'\n"
        "        config: { owner_ids: ['owner@lid'] }\n")
    cfg = loader.load(y)
    body = json.dumps({
        "event": "messages.upsert",
        "data": {
            "key": {
                "remoteJid": "120363@g.us",
                "id": "m1",
                "fromMe": False,
                "participant": "owner@lid",
            },
            "message": {"conversation": "status"},
        },
    }).encode()
    status, n = receiver.handle_webhook(conn, cfg, "whatsapp", body,
                                        {"x-argus-secret": "s3cret"})
    assert status == 200 and n == 1


def test_whatsapp_voice_transcribes_after_owner_gate(conn, tmp_path, monkeypatch):
    from argus.v2.channels import whatsapp

    y = tmp_path / "wa.yaml"
    y.write_text(
        "company:\n  name: c\n  defaults: { engine: { engine: echo }, webhook_secret: s3cret }\n"
        "teams:\n  - name: personal\n"
        "    roles: [ { name: manager, kind: front, prompt: p, engine: { engine: echo } } ]\n"
        "    pipeline: { stages: [manager] }\n"
        "    channels:\n"
        "      - type: whatsapp\n"
        "        role: control\n"
        "        channel_id: '120363@g.us'\n"
        "        config: { owner_ids: ['owner@lid'] }\n")
    cfg = loader.load(y)
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"voice-bytes")
    monkeypatch.setenv("ARGUS_RUN_ROOT", str(tmp_path / "run"))
    monkeypatch.setenv("ARGUS_WA_VOICE", "1")
    monkeypatch.setattr(whatsapp, "fetch_voice", lambda message_id, binding: str(audio))
    monkeypatch.setattr(whatsapp, "transcribe_voice", lambda path: "status please")
    body = json.dumps({
        "event": "messages.upsert",
        "data": {
            "key": {
                "remoteJid": "120363@g.us",
                "id": "m1",
                "fromMe": False,
                "participant": "owner@lid",
            },
            "message": {"audioMessage": {"mimetype": "audio/ogg", "seconds": 4, "ptt": True}},
        },
    }).encode()

    status, n = receiver.handle_webhook(conn, cfg, "whatsapp", body,
                                        {"x-argus-secret": "s3cret"})

    assert status == 200 and n == 1
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text' FROM events WHERE dedup_key='m1'")
        assert cur.fetchone()[0] == "status please"
        cur.execute("SELECT bytes FROM media")
        assert cur.fetchone()[0] == len(b"voice-bytes")


# ---------------------------------------------------------------------------
# Whisper retry-language decision (regression: forced-Hebrew retry overwrote
# clean English with garbage). Drive _transcribe_whisper with the whisper calls
# stubbed so no model/audio is needed.
# ---------------------------------------------------------------------------

from argus.v2.channels import whatsapp


def _stub_whisper(monkeypatch, tmp_path, *, auto, lang, prob, retry="RETRIED-HE"):
    monkeypatch.setenv("ARGUS_WHISPER_BIN", "true")          # shutil.which finds it
    model = tmp_path / "m.bin"; model.write_text("x")
    monkeypatch.setenv("ARGUS_WHISPER_MODEL", str(model))
    monkeypatch.setattr(whatsapp.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(whatsapp, "_run_whisper_detect",
                        lambda *a, **k: (auto, lang, prob))
    monkeypatch.setattr(whatsapp, "_run_whisper",
                        lambda *a, **k: retry)               # only the retry path calls this


def test_whisper_trusts_confident_english(monkeypatch, tmp_path):
    """Clean English (high-confidence en) is returned as-is, NOT overwritten by a
    forced-Hebrew retry."""
    monkeypatch.setenv("ARGUS_WHISPER_RETRY_LANG", "he")
    wav = tmp_path / "v.wav"; wav.write_text("")
    _stub_whisper(monkeypatch, tmp_path, auto="ignore this marketing", lang="en", prob=0.98)
    assert whatsapp._transcribe_whisper(wav) == "ignore this marketing"


def test_whisper_retries_when_low_confidence(monkeypatch, tmp_path):
    """Low-confidence non-Hebrew detection on Hebrew audio falls back to forced
    Hebrew (retry result contains Hebrew script)."""
    monkeypatch.setenv("ARGUS_WHISPER_RETRY_LANG", "he")
    wav = tmp_path / "v.wav"; wav.write_text("")
    _stub_whisper(monkeypatch, tmp_path, auto="garbled", lang="en", prob=0.30,
                  retry="שלום")          # "שלום"
    assert whatsapp._transcribe_whisper(wav) == "שלום"


def test_whisper_keeps_confident_hebrew(monkeypatch, tmp_path):
    """Confident Hebrew detection is trusted without a retry round."""
    monkeypatch.setenv("ARGUS_WHISPER_RETRY_LANG", "he")
    wav = tmp_path / "v.wav"; wav.write_text("")
    he = "שלום עולם"
    _stub_whisper(monkeypatch, tmp_path, auto=he, lang="he", prob=0.9, retry="WRONG")
    assert whatsapp._transcribe_whisper(wav) == he


def test_whisper_retry_rejected_without_target_script(monkeypatch, tmp_path):
    """If the forced retry still has no Hebrew, keep the auto text rather than
    swapping in junk."""
    monkeypatch.setenv("ARGUS_WHISPER_RETRY_LANG", "he")
    wav = tmp_path / "v.wav"; wav.write_text("")
    _stub_whisper(monkeypatch, tmp_path, auto="some english", lang="en", prob=0.2,
                  retry="still english")
    assert whatsapp._transcribe_whisper(wav) == "some english"


def test_parse_detected_lang():
    s = "whisper_full_with_state: auto-detected language: he (p = 0.857)"
    assert whatsapp._parse_detected_lang(s) == ("he", 0.857)
    assert whatsapp._parse_detected_lang("no language line here") == ("", 0.0)
