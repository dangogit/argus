"""Vercel preview discovery + UI-diff gating for the browser-verify stage.

Pure + injectable: no direct network or clock use in the hot path except through
the `http` and `sleep` parameters, so tests drive it deterministically.
"""

from __future__ import annotations

import fnmatch
from typing import Callable, Iterable, Optional

VERCEL_API = "https://api.vercel.com"

# States the Vercel deployments API reports. READY = the preview is servable.
_READY = {"READY"}
_DEAD = {"ERROR", "CANCELED"}


class PreviewError(Exception):
    """Base: the preview could not be obtained."""


class PreviewTimeout(PreviewError):
    """The preview did not reach READY within the build timeout."""


class PreviewFailed(PreviewError):
    """Vercel reported the deployment ERROR/CANCELED, or none was found."""


def _changed_paths(diff_text: str) -> set[str]:
    """Extract changed file paths from a unified git diff.

    Reads the `+++ b/<path>` / `--- a/<path>` headers. Ignores /dev/null (added
    or deleted files still surface their real side).
    """
    paths: set[str] = set()
    for line in diff_text.splitlines():
        for marker in ("+++ b/", "--- a/"):
            if line.startswith(marker):
                p = line[len(marker):].strip()
                if p and p != "/dev/null":
                    paths.add(p)
    return paths


def diff_touches_ui(diff_text: str, globs: Iterable[str]) -> bool:
    """True if any changed file matches a UI glob.

    fnmatch's `*` spans `/`, so `**/*.vue`, `src/views/**`, `**/*.css` all match
    as intended without a recursive-glob library.
    """
    globs = list(globs)
    for path in _changed_paths(diff_text):
        for glob in globs:
            if fnmatch.fnmatch(path, glob):
                return True
    return False


def _default_http(url: str, headers: dict) -> dict:  # pragma: no cover - network
    """Real HTTP GET returning parsed JSON. Injected out in tests."""
    import json
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_preview_url(
    *,
    project_id: str,
    branch: str,
    token: str,
    build_timeout_seconds: int = 300,
    poll_interval_seconds: int = 10,
    http: Callable[[str, dict], dict] = _default_http,
    sleep: Callable[[float], None] = None,
    commit: Optional[str] = None,
) -> str:
    """Poll the Vercel deployments API for the branch's preview and return its URL.

    Raises PreviewFailed if the newest matching deployment is ERROR/CANCELED or
    none exists, PreviewTimeout if it never reaches READY within the budget.
    `http(url, headers) -> parsed json` and `sleep(seconds)` are injectable.
    """
    if not project_id:
        raise PreviewFailed("no vercel project_id configured")
    if not token:
        raise PreviewFailed("no vercel token available")
    if sleep is None:  # pragma: no cover - trivial default
        import time

        sleep = time.sleep

    headers = {"Authorization": f"Bearer {token}"}
    query = (
        f"{VERCEL_API}/v6/deployments"
        f"?projectId={project_id}&meta-githubCommitRef={branch}&limit=1&target=preview"
    )

    # attempts covers the whole build budget; +1 so a 0s interval still polls once.
    attempts = max(1, build_timeout_seconds // max(1, poll_interval_seconds)) + 1
    last_state = "NONE"
    for attempt in range(attempts):
        data = http(query, headers)
        deployments = (data or {}).get("deployments") or []
        if deployments:
            dep = deployments[0]
            state = dep.get("readyState") or dep.get("state") or "UNKNOWN"
            last_state = state
            if state in _READY:
                url = dep.get("url")
                if not url:
                    raise PreviewFailed("deployment READY but has no url")
                return url if url.startswith("http") else f"https://{url}"
            if state in _DEAD:
                raise PreviewFailed(f"preview deployment {state.lower()}")
        if attempt < attempts - 1:
            sleep(poll_interval_seconds)
    raise PreviewTimeout(
        f"preview not READY after {build_timeout_seconds}s (last state: {last_state})"
    )
