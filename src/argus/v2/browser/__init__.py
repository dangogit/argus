"""Browser-verify support: Vercel preview discovery + UI-diff gating.

See docs/browser-verify-design.md. These helpers are pure and injectable so the
worker/pipeline wiring (which touches the shared state machine) can be added and
tested separately.
"""

from argus.v2.browser.preview import (
    PreviewError,
    PreviewFailed,
    PreviewTimeout,
    diff_touches_ui,
    discover_preview_url,
    discover_preview_url_firebase,
)
from argus.v2.browser.runner import (
    BrowserCheckResult,
    build_task,
    parse_verdict,
    run_browser_check,
)

__all__ = [
    "PreviewError",
    "PreviewFailed",
    "PreviewTimeout",
    "diff_touches_ui",
    "discover_preview_url",
    "discover_preview_url_firebase",
    "BrowserCheckResult",
    "build_task",
    "parse_verdict",
    "run_browser_check",
]
