"""Per-project eval rubric for the PR draft->ready gate. A project may ship its
own `evals/rubric.md`; otherwise the bundled default below is used.

Write the rubric BEFORE relying on the gate (eval-driven): it defines what
"good enough to promote" means. Keep it a regression rubric -- expect near-100%
on healthy PRs; a drop is breakage to investigate, not a threshold to lower.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_RUBRIC = """\
- correctness: the change does what its title/description claims, with no obvious
  logic error introduced.
- security: no secret/credential added to the diff; input at trust boundaries is
  validated; no new injection/path-traversal surface.
- spec_adherence: the diff stays within the stated scope; no unrelated churn.
- code_quality: matches surrounding style; errors handled where data loss is
  possible; no dead code left behind.
"""


def load(repo: Path) -> str:
    custom = Path(repo) / "evals" / "rubric.md"
    if custom.is_file():
        return custom.read_text(encoding="utf-8")
    return DEFAULT_RUBRIC
