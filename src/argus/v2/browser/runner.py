"""browser-use runner for the browser_verify stage.

Uses browser-use (https://github.com/browser-use/browser-use): an LLM browser
agent that drives the page itself, so we hand it a task + the preview URL and read
a PASS/FAIL verdict rather than scripting Playwright by hand.

The real browser run lives behind an injectable `runner` seam so unit tests need
neither browser-use, chromium, nor an LLM key. The default runner is finalized
against the installed browser-use version at enablement time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from argus.v2.browser._bu_run import RESULT_END as _RESULT_END
from argus.v2.browser._bu_run import RESULT_START as _RESULT_START

# Default Claude model for the browser agent. Sonnet is fast + capable enough for
# UI verification; override via config.
DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass
class BrowserCheckResult:
    verdict: str  # 'pass' | 'fail'
    reason: str
    raw: str


def _first_reason(raw: str) -> str:
    line = raw.splitlines()[0].strip() if raw.strip() else ""
    low = line.lower()
    for tok in ("pass", "fail"):
        if low.startswith(tok):
            return line[len(tok):].lstrip(" :-").strip() or line
    return line


def parse_verdict(text: str) -> BrowserCheckResult:
    """Parse a PASS/FAIL verdict from the browser agent's final result.

    FAIL-CLOSED: anything not clearly a PASS is a fail. A broken, ambiguous, or
    empty run must never silently approve a UI change.
    """
    raw = (text or "").strip()
    if not raw:
        return BrowserCheckResult("fail", "no verdict produced", raw)
    upper = raw.upper()
    first = upper.splitlines()[0]
    # Prefer an explicit token on the first line.
    if first.startswith("FAIL") or "FAIL" in first:
        return BrowserCheckResult("fail", _first_reason(raw), raw)
    if first.startswith("PASS") or "PASS" in first:
        return BrowserCheckResult("pass", _first_reason(raw), raw)
    # Fall back to a single unambiguous token anywhere.
    has_pass, has_fail = "PASS" in upper, "FAIL" in upper
    if has_pass and not has_fail:
        return BrowserCheckResult("pass", _first_reason(raw), raw)
    if has_fail and not has_pass:
        return BrowserCheckResult("fail", _first_reason(raw), raw)
    return BrowserCheckResult("fail", raw[:200], raw)


def build_task(
    *,
    url: str,
    changed_files: Sequence[str],
    summary: str,
    test_login: Optional[dict] = None,
) -> str:
    """Build the verification task prompt for the browser agent."""
    files = "\n".join(f"- {f}" for f in list(changed_files)[:20]) or "- (unknown)"
    login = ""
    if test_login and test_login.get("phone") and test_login.get("otp"):
        login = (
            f"\nIf a login is required, use phone {test_login['phone']} and OTP "
            f"code {test_login['otp']}."
        )
    return (
        f"Open {url} and verify a UI change on a preview deployment.\n\n"
        f"Change summary: {summary}\n\n"
        f"Changed files:\n{files}\n\n"
        "Steps: load the page, navigate to the screen affected by the change, and "
        "confirm it renders correctly and works (the described UI is present, no "
        "obvious layout breakage, no visible error state, no console-fatal crash)."
        f"{login}\n\n"
        "When done, output your verdict as the FIRST word on the FINAL line: "
        "exactly 'PASS' or 'FAIL', followed by a one-line reason. "
        "If you cannot load or exercise the screen, output FAIL."
    )


def _default_runner(
    *,
    task: str,
    allowed_domains: Sequence[str],
    model: str,
    timeout_seconds: int,
    browser_venv_python: Optional[str] = None,
) -> str:  # pragma: no cover - needs browser-use + chromium + LLM key
    """Run a headless browser-use agent and return its final result text.

    Preferred: `browser_venv_python` points at a DEDICATED venv that has
    browser-use, and we subprocess into _bu_run.py there - so the production argus
    runtime never imports browser-use (avoids dep conflicts). If it is not set we
    fall back to importing browser-use in-process (requires it in this venv).
    Validated against browser-use 0.13.1.
    """
    import json
    import subprocess
    from pathlib import Path

    if browser_venv_python:
        script = str(Path(__file__).with_name("_bu_run.py"))
        payload = json.dumps({
            "task": task,
            "allowed_domains": list(allowed_domains),
            "model": model,
            "timeout": timeout_seconds,
        })
        proc = subprocess.run(
            [browser_venv_python, script],
            input=payload, capture_output=True, text=True,
            timeout=timeout_seconds + 90,  # child has its own wait_for; +buffer for chromium startup
        )
        out = proc.stdout or ""
        start, end = out.find(_RESULT_START), out.find(_RESULT_END)
        if start != -1 and end != -1:
            return out[start + len(_RESULT_START):end].strip()
        raise RuntimeError(
            f"browser child produced no result (rc={proc.returncode}): "
            f"{(proc.stderr or out)[-500:]}"
        )

    # In-process fallback (browser-use must be importable in this venv).
    import asyncio

    from browser_use import Agent, BrowserProfile, ChatAnthropic, ChatOpenAI

    def _make_llm(m: str):
        name = m.split("/")[-1]
        if name.lower().startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
            return ChatOpenAI(model=name, temperature=0.0)
        return ChatAnthropic(model=name, temperature=0.0)

    async def _run() -> str:
        agent = Agent(
            task=task,
            llm=_make_llm(model),
            browser_profile=BrowserProfile(
                headless=True,
                allowed_domains=list(allowed_domains),
            ),
        )
        history = await asyncio.wait_for(agent.run(max_steps=12), timeout=timeout_seconds)
        return history.final_result() or ""

    return asyncio.run(_run())


def run_browser_check(
    *,
    preview_url: str,
    base_path: str = "/",
    changed_files: Sequence[str],
    summary: str,
    allowed_domains: Sequence[str],
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = 180,
    test_login: Optional[dict] = None,
    browser_venv_python: Optional[str] = None,
    runner: Callable[..., str] = _default_runner,
) -> BrowserCheckResult:
    """Drive a browser-use agent against the preview and return a PASS/FAIL verdict.

    Any exception from the run is treated as FAIL (fail-closed) so a crashed
    browser run cannot approve a change.
    """
    url = preview_url.rstrip("/") + (base_path if base_path.startswith("/") else "/" + base_path)
    task = build_task(url=url, changed_files=changed_files, summary=summary, test_login=test_login)
    try:
        raw = runner(
            task=task,
            allowed_domains=list(allowed_domains),
            model=model,
            timeout_seconds=timeout_seconds,
            browser_venv_python=browser_venv_python,
        )
    except Exception as exc:  # noqa: BLE001 - any failure = fail-closed
        return BrowserCheckResult("fail", f"browser run error: {exc}", str(exc))
    return parse_verdict(raw)
