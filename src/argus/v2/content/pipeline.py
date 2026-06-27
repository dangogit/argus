"""Content draft pipeline for queued briefs."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Callable

from argus.engine import run_agent
from argus.v2 import contracts
from argus.v2.content import state

EngineRunner = Callable[[str], str]
ImageGenerator = Callable[[str, Path, str, Path], None]


def draft(engine: str, project: str, platform: str, request: str, *,
          voice: str = "", image_driver: str = "echo", aspect: str = "1:1",
          cap: int = 5, engine_runner: EngineRunner | None = None,
          image_generator: ImageGenerator | None = None) -> str:
    state.breaker_check(project, cap)
    state.breaker_record(project)
    draft_id = state.register(project, platform)
    draft_dir = state.content_dir() / draft_id
    runner = engine_runner or (lambda prompt: run_agent(engine, prompt).text)
    generator = image_generator or generate_image

    brief = _strategist(runner, project, platform, request)
    (draft_dir / "brief.json").write_text(brief, encoding="utf-8")

    copy = _copywriter(runner, project, platform, brief, voice)
    (draft_dir / "copy.json").write_text(copy, encoding="utf-8")

    if image_driver != "none":
        prompt = _designer(runner, project, brief, copy)
        prompt_file = draft_dir / "image.prompt"
        prompt_file.write_text(prompt, encoding="utf-8")
        generator(image_driver, prompt_file, aspect, draft_dir / "image.png")

    review = _reviewer(runner, project, brief, copy, voice)
    (draft_dir / "review.json").write_text(review, encoding="utf-8")
    if _verdict(review) == "revise":
        notes = _json_field(review, "notes")
        revised_brief = f"{brief}\n\nReviewer notes to fix: {notes}"
        copy = _copywriter(runner, project, platform, revised_brief, voice)
        (draft_dir / "copy.json").write_text(copy, encoding="utf-8")
        review = _reviewer(runner, project, brief, copy, voice)
        (draft_dir / "review.json").write_text(review, encoding="utf-8")

    (draft_dir / "copy.txt").write_text(_json_field(copy, "body"), encoding="utf-8")
    return draft_id


def generate_image(driver: str, prompt_file: Path, aspect: str, out_file: Path) -> None:
    if driver == "echo":
        out_file.parent.mkdir(parents=True, exist_ok=True)
        prompt = prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else ""
        out_file.write_text(f"ARGUS-ECHO-IMAGE\naspect={aspect}\nprompt={prompt}\n", encoding="utf-8")
        return
    if driver == "gemini":
        _gemini_image(prompt_file, aspect, out_file)
        return
    raise ValueError(f"unknown image driver: {driver}")


def _gemini_image(prompt_file: Path, aspect: str, out_file: Path) -> None:
    import httpx

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("gemini image driver: set GEMINI_API_KEY")
    model = os.environ.get("ARGUS_GEMINI_IMAGE_MODEL", "gemini-3-pro-image")
    guard = (
        "Production-grade, editorial quality. Concrete scene. Avoid stock-photo "
        "cliche, generic AI look, cosmic or hologram glow, neon gradient blobs. "
        "No text in the image unless asked."
    )
    prompt = f"{prompt_file.read_text(encoding='utf-8')}\n\nComposition: {aspect} aspect ratio.\n\n{guard}"
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}},
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    b64 = next((part.get("inlineData", {}).get("data") for part in parts
                if part.get("inlineData", {}).get("data")), "")
    if not b64:
        raise RuntimeError("gemini image driver: no image in response")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(base64.b64decode(b64))


def _strategist(runner: EngineRunner, project: str, platform: str, request: str) -> str:
    contract = _contract("strategist")
    prompt = f"{contract}\n\nProject: {project}\nPlatform: {platform}\nOwner request: {request}\nProduce the brief."
    return runner(prompt)


def _copywriter(runner: EngineRunner, project: str, platform: str, brief: str, voice: str) -> str:
    contract = _contract("copywriter")
    prompt = f"{contract}\n\nProject: {project}\nPlatform: {platform}\nBrief:\n{brief}\nWrite the post (use every key point)."
    if voice:
        prompt = f"{prompt}\n\nBrand voice:\n{voice}"
    return runner(prompt)


def _designer(runner: EngineRunner, project: str, brief: str, copy: str) -> str:
    contract = _contract("designer")
    prompt = f"{contract}\n\nProject: {project}\nBrief:\n{brief}\n\nFinal copy:\n{copy}\n\nWrite the image prompt."
    return runner(prompt)


def _reviewer(runner: EngineRunner, project: str, brief: str, copy: str, voice: str) -> str:
    contract = _contract("reviewer")
    prompt = f"{contract}\n\nProject: {project}\nBrief:\n{brief}\n\nCopy:\n{copy}\n\nReturn the verdict."
    if voice:
        prompt = f"{prompt}\n\nBrand voice:\n{voice}"
    return runner(prompt)


def _contract(role: str) -> str:
    root = os.environ.get("ARGUS_CONTENT_CONTRACT_DIR")
    if root:
        path = Path(os.path.expandvars(root)).expanduser() / f"{role}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
    return contracts.CONTENT.get(role, "")


def _json_field(raw: str, field: str) -> str:
    try:
        parsed = json.loads(raw or "")
    except json.JSONDecodeError:
        return ""
    value = parsed.get(field) if isinstance(parsed, dict) else ""
    return value if isinstance(value, str) else ""


def _verdict(raw: str) -> str:
    return _json_field(raw, "verdict") or "pass"
