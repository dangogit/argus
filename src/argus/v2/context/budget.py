"""Token-budget fitting for assembled context bundles.

No tokenizer dependency: tokens are estimated as chars // 4, which is close
enough for the purpose (staying well clear of a model's context window).
When a bundle is over budget, categories are dropped in a fixed order:
knowledge results first, then oldest summaries, then oldest messages. The
most recent messages are never dropped below a floor, so a team never loses
all short-term context even under a very tight budget.
"""
from __future__ import annotations

import logging

log = logging.getLogger("argus.context")

MIN_RECENT_MESSAGES = 5


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def _bundle_tokens(bundle) -> int:
    return estimate_tokens(bundle.as_prompt())


def fit_to_budget(bundle, budget_tokens: int, *, min_recent_messages: int = MIN_RECENT_MESSAGES):
    """Trim bundle in place (returns the same instance) until it fits
    budget_tokens, following the fixed degrade order: knowledge, then oldest
    summaries, then oldest messages (never below min_recent_messages).
    Logs one INFO line if anything was dropped."""
    before_tokens = _bundle_tokens(bundle)
    if before_tokens <= budget_tokens:
        return bundle

    dropped_knowledge = 0
    dropped_summaries = 0
    dropped_messages = 0

    while bundle.knowledge and _bundle_tokens(bundle) > budget_tokens:
        bundle.knowledge.pop()
        dropped_knowledge += 1

    # summaries are ordered oldest-first (assemble() reverses the DESC query),
    # so drop from the front to remove the oldest first.
    while bundle.summaries and _bundle_tokens(bundle) > budget_tokens:
        bundle.summaries.pop(0)
        dropped_summaries += 1

    # recent_messages are ordered oldest-first; keep at least the floor.
    while (len(bundle.recent_messages) > min_recent_messages
           and _bundle_tokens(bundle) > budget_tokens):
        bundle.recent_messages.pop(0)
        dropped_messages += 1

    if dropped_knowledge or dropped_summaries or dropped_messages:
        after_tokens = _bundle_tokens(bundle)
        log.info(
            "context budget trim: dropped knowledge=%d summaries=%d messages=%d "
            "tokens before=%d after=%d budget=%d",
            dropped_knowledge, dropped_summaries, dropped_messages,
            before_tokens, after_tokens, budget_tokens,
        )
    return bundle
