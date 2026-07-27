"""Model-augmented rationale for selected agenda items.

Per ``_shared/autonomy-kernel-protocol.md §Rationale``: for every item
the kernel selects, a single-turn, bounded model call generates the
operator-facing "why now vs the alternatives?" explanation. The
rationale is logged + persisted to the item's ``last_decision`` field
but **never** re-weights or re-orders selection — the chat model
explains the deterministic decision, it does not make it.

Failure-tolerant: if the adapter raises, times out, or is missing
entirely, the kernel proceeds with selection. The rationale falls back
to ``model_rationale_unavailable`` so the dashboard surfaces the
absence cleanly.

The adapter is injectable. Production wiring resolves
``roles.yaml::roles.agents_default`` (per AU-5 phase doc §Session 2);
tests pass a mock that returns a deterministic string.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from tesseract.orchestrator.autonomy.models import AgendaItem

log = logging.getLogger(__name__)


# Default token budget. Anything above 2000 tokens of rationale is a
# code smell — the operator-facing card cuts off long before that.
DEFAULT_MAX_OUTPUT_TOKENS = 2000
DEFAULT_TIMEOUT_SECONDS = 15.0
UNAVAILABLE_MARKER = "model_rationale_unavailable"


class RationaleAdapter(Protocol):
    """Minimal interface a model adapter must satisfy. Production
    code passes ``ModelAdapter.generate``; tests pass a coroutine that
    returns a fixed string."""

    async def __call__(self, prompt: str) -> str: ...  # pragma: no cover


def build_prompt(item: AgendaItem, peers: list[AgendaItem]) -> str:
    """Return the single-turn prompt. Deterministic — same item +
    same peer list yields the same prompt, so a future cache layer
    can be keyed off ``hash(prompt)`` without surprises."""
    score_lines = [
        f"  {k}={v:.2f}" for k, v in sorted(item.score_components.items())
    ]
    peer_lines = [
        f"  - {p.id} ({p.priority_score:.2f}) — {p.goal[:120]}"
        for p in peers[:5]
        if p.id != item.id
    ]
    return (
        "Why are we working on this NOW vs other backlog items?\n\n"
        f"Item: {item.goal}\n"
        f"Risk class: {item.risk_class.value}\n"
        f"Source: {item.source.value}\n"
        f"Score: {item.priority_score:.2f}\n"
        "Score breakdown:\n" + "\n".join(score_lines) + "\n\n"
        "Top alternatives:\n" + ("\n".join(peer_lines) or "  (none)") + "\n\n"
        "Reply in 2-4 sentences. Be specific about which score components "
        "pushed this item to the front. No preamble."
    )


async def generate_rationale(
    item: AgendaItem,
    peers: list[AgendaItem],
    *,
    adapter: RationaleAdapter | None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_TOKENS * 4,
) -> str:
    """Call ``adapter`` once with the per-item prompt. Returns the
    raw rationale string or :data:`UNAVAILABLE_MARKER` on any failure.

    ``max_output_chars`` caps the persisted string so a runaway model
    response cannot blow the AgendaItem ``rationale`` field's 2000-char
    limit on save."""
    if adapter is None:
        return UNAVAILABLE_MARKER
    prompt = build_prompt(item, peers)
    try:
        result = await asyncio.wait_for(adapter(prompt), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        log.warning("autonomy: rationale generation timed out for %s", item.id)
        return UNAVAILABLE_MARKER
    except Exception:
        log.exception("autonomy: rationale generation raised for %s", item.id)
        return UNAVAILABLE_MARKER
    if not isinstance(result, str) or not result.strip():
        return UNAVAILABLE_MARKER
    return result.strip()[:max_output_chars]


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "RationaleAdapter",
    "UNAVAILABLE_MARKER",
    "build_prompt",
    "generate_rationale",
]
