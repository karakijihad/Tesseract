"""AU-16 S1 — ``ExtractChunkJob``.

Picks up every leaf currently in ``LeafState.PENDING_EXTRACTION``,
normalises whitespace, sniffs ``[[wikilinks]]`` into entities, scores
importance with a small heuristic, and transitions the leaf to
``ADMITTED`` or ``DROPPED``.

No LLM in S1 — the extraction is purely lexical. A future iteration can
swap in a model-driven classifier; the job contract (read leaves in
``pending_extraction`` → mutate body/entities/importance → transition)
won't change.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from tesseract.memory.leaves import LeafState, LeafStore
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

# Anything shorter than ``MIN_LEAF_CHARS`` after whitespace-normalisation
# is dropped — it carries no signal once buffered + sealed. ``MAX_LEAF_CHARS``
# caps the body before importance scoring so a runaway agent transcript
# can't dominate the seal summary.
MIN_LEAF_CHARS = 40
MAX_LEAF_CHARS = 20_000

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_WS_RE = re.compile(r"[ \t]+")

# Heuristic capitalised-token sniff. Catches `[A-Z][A-Za-z0-9][\w.-]{2,}` runs and drops the
# small set of starts-with-uppercase common words ("The", "When", …)
# that would otherwise flood the topic-tree.
_CAP_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9][\w.-]{2,}\b")
_CAP_STOPWORDS = frozenset(
    {
        "The", "This", "That", "There", "These", "Those",
        "When", "Where", "What", "Which", "While", "With",
        "From", "About", "Into", "Over",
        "Their", "They", "Your", "His", "Her",
        "After", "Before", "Today", "Tomorrow", "Yesterday",
        "Assistant", "User", "Operator", "TARS",
    }
)


def normalise_body(body: str) -> str:
    """Collapse repeated whitespace runs, strip trailing whitespace per
    line, and cap at ``MAX_LEAF_CHARS``. Newlines are preserved (markdown
    paragraph breaks matter)."""
    cleaned_lines: list[str] = []
    for line in body.splitlines():
        compact = _WS_RE.sub(" ", line).rstrip()
        cleaned_lines.append(compact)
    # Drop trailing blank lines.
    while cleaned_lines and not cleaned_lines[-1]:
        cleaned_lines.pop()
    joined = "\n".join(cleaned_lines).strip()
    return joined[:MAX_LEAF_CHARS]


def extract_entities(body: str) -> list[str]:
    """Pull entity tokens from the leaf body.

    Two sources, in order:

    1. Explicit ``[[wikilinks]]`` (operator-curated cross-references).
    2. Heuristic capitalised-token sniff.

    Order-preserved + case-sensitive deduped. Common-word stopwords
    drop out so "The", "When", etc. don't activate spurious topics.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in _WIKILINK_RE.findall(body):
        token = raw.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    for match in _CAP_ENTITY_RE.findall(body):
        if match in _CAP_STOPWORDS or match in seen:
            continue
        seen.add(match)
        out.append(match)
    return out


def score_importance(body: str, *, entities: list[str], base: int = 5) -> int:
    """Tiny heuristic: floor 1, ceiling 10. Body length + entity count
    nudge up; short bodies nudge down. S2 may swap this for the agenda
    scoring engine's pattern (component breakdown) once the trees consume
    a richer signal."""
    score = base
    n = len(body)
    if n >= 800:
        score += 2
    elif n >= 200:
        score += 1
    if n < 80:
        score -= 2
    if entities:
        score += 1
    return max(1, min(10, score))


class ExtractChunkJob(BaseJob):
    """Per-tick extraction pass.

    Configuration via ``ctx.config``:

    - ``max_per_tick``: cap on leaves processed in one invocation
      (default 64). Keeps the tick bounded so a large backlog can't
      lock the scheduler for minutes.
    - ``store_root``: optional override for the ``LeafStore`` root
      (tests inject ``tmp_path / "memory-store" / "leaves"``).
    """

    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        max_per_tick = int(ctx.config.get("max_per_tick", 64))
        store_root = ctx.config.get("store_root")
        store = LeafStore(root=Path(store_root) if store_root else None)

        processed = 0
        admitted = 0
        dropped = 0
        errors = 0

        for leaf in store.list_in_state(LeafState.PENDING_EXTRACTION):
            if processed >= max_per_tick:
                break
            processed += 1
            try:
                cleaned = normalise_body(leaf.body)
                entities = extract_entities(cleaned)
                importance = score_importance(cleaned, entities=entities)
                if len(cleaned) < MIN_LEAF_CHARS:
                    store.transition(
                        leaf,
                        LeafState.DROPPED,
                        reason=f"too_short:{len(cleaned)}<{MIN_LEAF_CHARS}",
                    )
                    dropped += 1
                    continue
                leaf.body = cleaned
                leaf.entities = entities
                leaf.importance = importance
                store.transition(leaf, LeafState.ADMITTED, reason="extracted")
                admitted += 1
            except Exception:
                log.exception(
                    "extract_chunk: leaf %s raised — leaving in pending",
                    leaf.id,
                )
                errors += 1

        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=(
                f"processed={processed} admitted={admitted} dropped={dropped} "
                f"errors={errors}"
            ),
            payload={
                "processed": processed,
                "admitted": admitted,
                "dropped": dropped,
                "errors": errors,
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


__all__ = [
    "ExtractChunkJob",
    "MAX_LEAF_CHARS",
    "MIN_LEAF_CHARS",
    "extract_entities",
    "normalise_body",
    "score_importance",
]
