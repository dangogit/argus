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
)

__all__ = [
    "PreviewError",
    "PreviewFailed",
    "PreviewTimeout",
    "diff_touches_ui",
    "discover_preview_url",
]
