"""AU-16 S2 — ``TopicRouteJob``.

Scans every seal artefact, counts entity occurrences across the leaves
each seal sealed, and:

- Activates a topic tree (creates the file) when an entity's lifetime
  occurrence count crosses ``TOPIC_ACTIVATION_THRESHOLD``.
- Appends each seal to every active topic tree whose entity is among
  the seal's leaf entities.

The job is idempotent — re-running it never duplicates a seal section
on a topic tree (the writer skips on seal_id match). Activation is also
idempotent (returns the existing path).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

from tesseract.memory.leaf_seals import Seal, iter_seals
from tesseract.memory.leaves import LeafStore, MemoryLeaf
from tesseract.memory.trees.topic_tree import (
    TOPIC_ACTIVATION_THRESHOLD,
    activate_topic,
    append_seal,
    is_topic_active,
)
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


def _resolve_leaf_entities(
    seal: Seal, *, store: LeafStore
) -> dict[str, list[str]]:
    """Map leaf_id → entity list. Looks up each leaf via ``LeafStore``;
    missing leaves contribute no entities (the seal is still counted but
    won't activate any topic by itself)."""
    out: dict[str, list[str]] = {}
    for leaf_id in seal.leaf_ids:
        leaf = store.get(leaf_id)
        if leaf is None:
            out[leaf_id] = []
            continue
        out[leaf_id] = list(leaf.entities)
    return out


class TopicRouteJob(BaseJob):
    """Per-tick topic-tree maintenance.

    Configuration via ``ctx.config``:

    - ``threshold``: occurrence floor for topic activation
      (default ``TOPIC_ACTIVATION_THRESHOLD``).
    - ``store_root``: optional ``LeafStore`` root override.
    """

    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        threshold = int(ctx.config.get("threshold", TOPIC_ACTIVATION_THRESHOLD))
        store_root = ctx.config.get("store_root")
        store = LeafStore(root=Path(store_root) if store_root else None)

        seals = list(iter_seals())
        # Lifetime entity counter — used to decide which topics to activate.
        entity_counter: Counter[str] = Counter()
        per_seal_entities: list[tuple[Seal, dict[str, list[str]]]] = []

        for seal in seals:
            leaf_entities = _resolve_leaf_entities(seal, store=store)
            per_seal_entities.append((seal, leaf_entities))
            for entities in leaf_entities.values():
                for entity in entities:
                    entity_counter[entity] += 1

        activated: list[str] = []
        for entity, count in entity_counter.items():
            if count >= threshold and not is_topic_active(entity):
                activate_topic(entity)
                activated.append(entity)

        # Append every seal section to every active topic the seal touches.
        # ``append_seal`` returns True only when a fresh section landed;
        # idempotent skips bump ``sections_skipped`` so the operator can
        # tell whether a re-run actually wrote anything.
        sections_written = 0
        sections_skipped = 0
        for seal, leaf_entities in per_seal_entities:
            seal_entities = {e for ents in leaf_entities.values() for e in ents}
            for entity in seal_entities:
                if not is_topic_active(entity):
                    continue
                try:
                    if append_seal(entity, seal, leaf_entities=leaf_entities):
                        sections_written += 1
                    else:
                        sections_skipped += 1
                except Exception:
                    log.exception(
                        "topic_route: failed to append seal %s to topic %s",
                        seal.seal_id,
                        entity,
                    )

        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail=(
                f"seals_scanned={len(seals)} entities={len(entity_counter)} "
                f"activated={len(activated)} sections_written={sections_written} "
                f"sections_skipped={sections_skipped}"
            ),
            payload={
                "seals_scanned": len(seals),
                "entities": len(entity_counter),
                "activated": activated,
                "sections_written": sections_written,
                "sections_skipped": sections_skipped,
            },
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )


__all__ = ["TopicRouteJob"]
