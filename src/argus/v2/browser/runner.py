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
) -> str:  # pragma: no cover - needs browser-use + chromium + LLM key
    """Run a headless browser-use agent and return its final result text.

    Lazy import so the package is only required when the stage actually runs.
    Finalize the exact call against the installed browser-use version; the beta
    API below supports headless + allowed_domains (domain allowlist keeps the
    agent confined to the preview + its API host).
    """
    import asyncio

    from browser_use.beta import Agent, BrowserProfile, ChatAnthropic

    async def _run() -> str:
        agent = Agent(
            task=task,
            llm=ChatAnthropic(model=model, temperature=0.0),
            browser_profile=BrowserProfile(
                headless=True,
                allowed_domains=list(allowed_domains),
            ),
        )
        history = await agent.run()
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
        )
    except Exception as exc:  # noqa: BLE001 - any failure = fail-closed
        return BrowserCheckResult("fail", f"browser run error: {exc}", str(exc))
    return parse_verdict(raw)
