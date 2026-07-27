"""First-boot AgendaStore bootstrap.

Writes a single ``status=done`` agenda item the first time the store
runs on a clean ``<TESSERACT_HOME>``. Reasons:

- Dashboard empty-state has something to render so the operator can
  verify the pipeline end-to-end on first boot.
- The audit log (``index.jsonl``) has at least one entry so post-restart
  scans confirm the file exists with the right format.

Idempotent: a sentinel file at ``<TESSERACT_HOME>/agenda/.bootstrap`` is
written after the seed lands. Subsequent boots short-circuit on the
sentinel even if the bootstrap item itself was archived / deleted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.autonomy.paths import agenda_root

log = logging.getLogger(__name__)

_BOOTSTRAP_GOAL = "agenda-bootstrap-seed"
_BOOTSTRAP_RATIONALE = "First-boot seed so the dashboard's empty state has something to render."


def bootstrap_agenda(store: AgendaStore | None = None) -> AgendaItem | None:
    """Write a seed item if the sentinel is absent. Returns the seeded
    item, or ``None`` when the sentinel was already in place."""
    root = agenda_root()
    sentinel = root / ".bootstrap"
    if sentinel.exists():
        return None
    store = store or AgendaStore()

    now = datetime.now(timezone.utc)
    item = AgendaItem(
        id=mint_agenda_id("bootstrap-seed", now=now),
        created_at=now,
        updated_at=now,
        source=AgendaSource.RECOVERY,
        goal=_BOOTSTRAP_GOAL,
        rationale=_BOOTSTRAP_RATIONALE,
        risk_class=RiskClass.AUTONOMOUS,
        status=AgendaStatus.PROPOSED,
    )
    store.add(item, by="recovery", reason="bootstrap_seed")
    # Drive to done so it lands in the archive view, not the active queue.
    store.transition(
        item,
        AgendaStatus.DONE,
        reason="bootstrap_seed_completed",
        by="recovery",
    )

    root.mkdir(parents=True, exist_ok=True)
    try:
        sentinel.write_text(now.isoformat(), encoding="utf-8")
    except OSError:
        log.exception("agenda: bootstrap sentinel write failed (will retry on next boot)")
    log.info("agenda: bootstrap seed %s written", item.id)
    return item


__all__ = ["bootstrap_agenda"]
