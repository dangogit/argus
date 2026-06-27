"""WhatsApp via Evolution. parse_inbound (gate-tested) reads the messages.upsert
webhook; send/fetch_media (seams) hit the Evolution API."""
from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from argus.v2.channels.base import InboundMessage, register


def _text(msg: dict) -> str:
    if "conversation" in msg:
        return msg["conversation"]
    ext = msg.get("extendedTextMessage") or {}
    return ext.get("text", "")


def _sender_ref(data: dict) -> str:
    key = data.get("key") or {}
    return str(
        key.get("participant")
        or key.get("participantLid")
        or data.get("participant")
        or data.get("sender")
        or ""
    )


def _audio(msg: dict) -> dict | None:
    audio = msg.get("audioMessage")
    if not isinstance(audio, dict):
        return None
    return {
        "kind": "audio",
        "mime": str(audio.get("mimetype") or "audio/ogg"),
        "message_id": "",
        "ptt": bool(audio.get("ptt")),
        "seconds": int(audio.get("seconds") or 0),
    }


def split_text(text: str, max_chars: int, max_parts: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in (text or "").splitlines() or [""]:
        while len(line) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:max_chars])
            line = line[max_chars:]
        candidate = line if not current else current + "\n" + line
        if len(candidate) > max_chars:
            if current:
                parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return [part for part in parts if part][:max_parts]


@register
class WhatsAppChannel:
    type = "whatsapp"

    def parse_inbound(self, raw, secret=None):
        if not isinstance(raw, dict) or raw.get("event") != "messages.upsert":
            return []
        data = raw.get("data") or {}
        key = data.get("key") or {}
        if key.get("fromMe"):
            return []
        msg = data.get("message") or {}
        text = _text(msg)
        if not key.get("remoteJid") or not key.get("id"):
            return []
        media = []
        audio = _audio(msg)
        if audio:
            audio["message_id"] = str(key["id"])
            media.append(audio)
        return [InboundMessage(chat_id=key["remoteJid"], text=text,
                               dedup_key=key["id"], sender=data.get("pushName", ""),
                               sender_ref=_sender_ref(data), media=media)]

    def prepare_inbound(self, msg: InboundMessage, raw, binding) -> InboundMessage:
        if msg.text or os.environ.get("ARGUS_WA_VOICE") != "1":
            return msg
        audio = next((item for item in msg.media if item.get("kind") == "audio"), None)
        if not audio:
            return msg
        try:
            path = fetch_voice(str(audio.get("message_id") or msg.dedup_key), binding)
            transcript = transcribe_voice(path)
        except Exception:
            return msg
        max_chars = _int_env("ARGUS_WA_VOICE_MAX_CHARS", 2000)
        msg.text = _collapse(transcript)[:max_chars]
        audio["src"] = path
        return msg

    def send(self, binding, text: str) -> str:  # pragma: no cover
        import httpx

        cfg = binding.config or {}
        base, instance, apikey = _settings(binding)
        if cfg.get("presence") or os.environ.get("ARGUS_WA_PRESENCE") == "1":
            send_presence(binding)
        ids = []
        max_chars = int(cfg.get("max_chars") or os.environ.get("ARGUS_WA_MAX_CHARS") or 3800)
        max_parts = int(cfg.get("max_parts") or os.environ.get("ARGUS_WA_MAX_PARTS") or 8)
        for part in split_text(text, max_chars, max_parts):
            r = httpx.post(
                f"{base}/message/sendText/{instance}",
                headers={"apikey": apikey},
                json={"number": binding.channel_id, "text": part},
                timeout=_int_env("ARGUS_WA_SEND_TIMEOUT", 45),
            )
            r.raise_for_status()
            ids.append(str(r.json().get("key", {}).get("id", "")))
        return ids[0] if ids else ""


def send_presence(binding) -> bool:  # pragma: no cover
    import httpx

    try:
        base, instance, apikey = _settings(binding)
        response = httpx.post(
            f"{base}/chat/sendPresence/{instance}",
            headers={"apikey": apikey},
            json={"number": binding.channel_id, "presence": "composing", "delay": 1200},
            timeout=5,
        )
        response.raise_for_status()
        return True
    except Exception:
        return False


def fetch_voice(message_id: str, binding) -> str:  # pragma: no cover
    import httpx

    base, instance, apikey = _settings(binding)
    response = httpx.post(
        f"{base}/chat/getBase64FromMediaMessage/{instance}",
        headers={"apikey": apikey},
        json={"message": {"key": {"id": message_id}}, "convertToMp4": False},
        timeout=_int_env("ARGUS_WA_VOICE_TIMEOUT", 30),
    )
    response.raise_for_status()
    data = response.json()
    raw = base64.b64decode(str(data.get("base64") or ""))
    if not raw:
        raise RuntimeError("empty voice media")
    root = Path(os.environ.get("ARGUS_RUN_ROOT", "run")).expanduser() / "wa-voice"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{re.sub(r'[^A-Za-z0-9._-]', '_', message_id)}.ogg"
    path.write_bytes(raw)
    return str(path)


def transcribe_voice(path: str) -> str:  # pragma: no cover
    cmd = os.environ.get("ARGUS_WA_TRANSCRIBE_CMD")
    if cmd:
        proc = subprocess.run(
            shlex.split(cmd) + [path],
            capture_output=True,
            text=True,
            check=True,
        )
        return _collapse(proc.stdout)
    return _transcribe_whisper(Path(path))


def _transcribe_whisper(path: Path) -> str:
    whisper = os.environ.get("ARGUS_WHISPER_BIN", "whisper-cli")
    model = os.environ.get("ARGUS_WHISPER_MODEL", "")
    if not shutil.which(whisper):
        raise RuntimeError("whisper-cli not found")
    if not model or not Path(model).exists():
        raise RuntimeError("ARGUS_WHISPER_MODEL must point to a local ggml model")
    with tempfile.TemporaryDirectory(prefix="argus-transcribe-") as tmp:
        tmp_path = Path(tmp)
        wav = path
        if path.suffix.lower() != ".wav":
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg is required for voice transcription")
            wav = tmp_path / "voice.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-ar", "16000", "-ac", "1", str(wav)],
                check=True,
                capture_output=True,
            )
        forced = os.environ.get("ARGUS_WHISPER_LANG")
        if forced:
            return _run_whisper(whisper, model, wav, tmp_path / "forced", forced)
        auto, lang, prob = _run_whisper_detect(whisper, model, wav, tmp_path / "auto")
        retry_lang = os.environ.get("ARGUS_WHISPER_RETRY_LANG", "")
        # Fall back to a forced language ONLY when auto-detect is unreliable: it
        # produced nothing, or it picked some OTHER language with low confidence.
        # A confident non-fallback detection (e.g. clean English) is trusted as-is.
        # The old rule retried whenever auto merely lacked the fallback script and
        # then kept any script-containing retry, which overwrote good English with
        # forced-language garbage (real English voice note -> Hebrew, 2026-06-22).
        threshold = float(os.environ.get("ARGUS_WHISPER_RETRY_CONF", "0.6"))
        trust_auto = bool(auto) and (lang == retry_lang or prob >= threshold)
        if retry_lang and not trust_auto:
            retried = _run_whisper(whisper, model, wav, tmp_path / "retry", retry_lang, allow_empty=True)
            if retried and _contains_script(retried, retry_lang):
                return retried
        if not auto:
            raise RuntimeError("whisper produced no transcript")
        return auto


def _run_whisper(whisper: str, model: str, wav: Path, prefix: Path, lang: str,
                 *, allow_empty: bool = False) -> str:
    subprocess.run(
        [whisper, "-m", model, "-f", str(wav), "-otxt", "-of", str(prefix), "-nt", "-l", lang],
        check=not allow_empty,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    out = prefix.with_suffix(".txt")
    text = _collapse(out.read_text(encoding="utf-8")) if out.exists() else ""
    if not text and not allow_empty:
        raise RuntimeError("whisper produced no transcript")
    return text


def _run_whisper_detect(whisper: str, model: str, wav: Path,
                        prefix: Path) -> tuple[str, str, float]:
    """Run whisper in auto-detect mode; return (text, detected_lang, confidence).
    Confidence comes from whisper.cpp's stderr line 'auto-detected language: he
    (p = 0.85)'; ('', 0.0) when it can't be parsed. Never raises on empty."""
    proc = subprocess.run(
        [whisper, "-m", model, "-f", str(wav), "-otxt", "-of", str(prefix), "-nt", "-l", "auto"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    out = prefix.with_suffix(".txt")
    text = _collapse(out.read_text(encoding="utf-8")) if out.exists() else ""
    lang, prob = _parse_detected_lang(proc.stderr or "")
    return text, lang, prob


def _parse_detected_lang(stderr: str) -> tuple[str, float]:
    m = re.search(r"auto-detected language:\s*([A-Za-z]{2,3})\s*\(p\s*=\s*([0-9.]+)\)",
                  stderr or "")
    if not m:
        return "", 0.0
    return m.group(1).lower(), float(m.group(2))


def _contains_script(text: str, lang: str) -> bool:
    ranges = {
        "he": r"\u0590-\u05ff",
        "yi": r"\u0590-\u05ff",
        "ar": r"\u0600-\u06ff",
        "fa": r"\u0600-\u06ff",
        "ur": r"\u0600-\u06ff",
        "ru": r"\u0400-\u04ff",
        "uk": r"\u0400-\u04ff",
        "bg": r"\u0400-\u04ff",
        "sr": r"\u0400-\u04ff",
        "el": r"\u0370-\u03ff",
    }
    pattern = ranges.get(lang, r"\u0080-\uffff")
    return re.search(f"[{pattern}]", text or "") is not None


def _settings(binding) -> tuple[str, str, str]:
    cfg = binding.config or {}
    base = str(cfg.get("base_url") or os.environ.get("ARGUS_WA_URL") or "http://127.0.0.1:8080").rstrip("/")
    instance = str(cfg.get("instance") or os.environ.get("ARGUS_WA_INSTANCE") or "")
    apikey = str(binding.secret or cfg.get("apikey") or os.environ.get("ARGUS_WA_APIKEY") or "")
    if not apikey and os.environ.get("ARGUS_WA_APIKEY_FILE"):
        apikey = Path(os.environ["ARGUS_WA_APIKEY_FILE"]).read_text(encoding="utf-8").strip()
    if not instance or not apikey:
        raise RuntimeError("whatsapp transport not configured")
    return base, instance, apikey


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default
