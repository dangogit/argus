"""Daily content queue drain."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from argus.v2.content import pipeline, state

Notifier = Callable[[str, str, str, Path | None], bool]


@dataclass(frozen=True)
class DrainResult:
    draft_id: str = ""
    no_queue: bool = False
    failed: bool = False
    notified: bool = False


def run(*, engine_runner: pipeline.EngineRunner | None = None,
        image_generator: pipeline.ImageGenerator | None = None,
        notifier: Notifier | None = None) -> DrainResult:
    queued = state.queue_oldest()
    if not queued:
        return DrainResult(no_queue=True)
    queue_id = str(queued["id"])
    project = str(queued.get("project") or "")
    platform = str(queued.get("platform") or "")
    request = str(queued.get("request") or "")
    attempts = int(queued.get("attempts") or 0)
    try:
        draft_id = pipeline.draft(
            _engine(),
            project,
            platform,
            request,
            voice=os.environ.get("ARGUS_CONTENT_BRAND_VOICE", ""),
            image_driver=_image_driver(),
            aspect=os.environ.get("ARGUS_CONTENT_ASPECT", "1:1"),
            cap=_daily_limit(),
            engine_runner=engine_runner,
            image_generator=image_generator,
        )
    except Exception:
        attempts += 1
        if attempts >= 3:
            state.queue_set_status(queue_id, "dead", attempts)
            state.alert_warn(
                f"content-queue-{queue_id}",
                f"content drain: giving up on queued brief {queue_id} ({project}/{platform})",
            )
        else:
            state.queue_set_status(queue_id, "queued", attempts)
        return DrainResult(failed=True)

    state.queue_set_status(queue_id, "drafted")
    notified = _notify(draft_id, notifier or _default_notifier)
    return DrainResult(draft_id=draft_id, notified=notified)


def _notify(draft_id: str, notifier: Notifier) -> bool:
    draft_dir = state.content_dir() / draft_id
    copy = (draft_dir / "copy.txt").read_text(encoding="utf-8") if (draft_dir / "copy.txt").exists() else ""
    image = draft_dir / "image.png"
    image_path = image if image.exists() else None
    try:
        return notifier(draft_id, copy, _owner_destination(), image_path)
    except Exception:
        return False


def _default_notifier(draft_id: str, body: str, to: str, image: Path | None) -> bool:
    if not to:
        return False
    header = f'Draft {draft_id} ready. Reply "publish {draft_id}" to post, "reject {draft_id}" to drop.'
    _send_text(to, f"{header}\n\n{body}")
    if image is not None and image.exists():
        if not _send_media(to, f"draft {draft_id}", image):
            _send_text(to, f"image at: {image}")
    return True


def _send_text(to: str, text: str) -> bool:
    import httpx

    cfg = _wa_config()
    if not cfg:
        return False
    base, instance, apikey = cfg
    try:
        response = httpx.post(
            f"{base}/message/sendText/{instance}",
            headers={"apikey": apikey},
            json={"number": to, "text": text},
            timeout=20,
        )
        response.raise_for_status()
    except Exception:
        return False
    return True


def _send_media(to: str, caption: str, image: Path) -> bool:
    import httpx

    cfg = _wa_config()
    if not cfg:
        return False
    base, instance, apikey = cfg
    try:
        response = httpx.post(
            f"{base}/message/sendMedia/{instance}",
            headers={"apikey": apikey},
            json={
                "number": to,
                "mediatype": "image",
                "mimetype": "image/png",
                "caption": caption,
                "media": base64.b64encode(image.read_bytes()).decode("ascii"),
                "fileName": image.name,
            },
            timeout=30,
        )
        response.raise_for_status()
    except Exception:
        return False
    return True


def _wa_config() -> tuple[str, str, str] | None:
    base = os.environ.get("ARGUS_WA_URL", "").rstrip("/")
    instance = os.environ.get("ARGUS_WA_INSTANCE", "")
    apikey = os.environ.get("ARGUS_WA_APIKEY", "")
    if not apikey and os.environ.get("ARGUS_WA_APIKEY_FILE"):
        apikey = Path(os.path.expandvars(os.environ["ARGUS_WA_APIKEY_FILE"])).expanduser().read_text(
            encoding="utf-8"
        ).strip()
    if not base or not instance or not apikey:
        return None
    return base, instance, apikey


def _owner_destination() -> str:
    return (
        os.environ.get("ARGUS_WA_TO")
        or os.environ.get("ARGUS_WA_OWNER")
        or os.environ.get("WHATSAPP_PERSONAL_NUMBER")
        or ""
    )


def _engine() -> str:
    return os.environ.get("ARGUS_CONTENT_ENGINE") or os.environ.get("ARGUS_FALLBACK_ENGINE") or "codex"


def _image_driver() -> str:
    return os.environ.get("ARGUS_CONTENT_IMAGE_DRIVER", "echo")


def _daily_limit() -> int:
    try:
        return int(os.environ.get("ARGUS_CONTENT_DAILY_LIMIT", "5"))
    except ValueError:
        return 5
