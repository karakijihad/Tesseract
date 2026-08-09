"""TC-7 — auto follow-up mapper.

When a ``CODER_SEAT`` / ``AGENT_CONTROLLER`` advisor finishes with a
non-trivial ``record.summary`` and zero artifacts, the advisor produced
*advice*, not *work*. The kernel emits an ``advice_only`` row to the
operator journal, but there is no implicit next step — the operator
would have to draft the follow-up agenda item themselves, which they
forget. TC-7 closes that loop:

1. After ``_reconcile_agenda_for_worker`` writes the ``advice_only``
   row, the kernel calls :meth:`FollowUpMapper.create_draft_if_actionable`
2. The mapper runs a deterministic heuristic (length floor + keyword
   whitelist from ``agenda.yaml::follow_up_mapper``) — NOT an LLM call
3. If actionable, it mints an :class:`AgendaItem` with
   ``status = AWAITING_OPERATOR`` and an ``operator_review`` approval
   gate. The item lands in the Mirror Approvals pane via the existing
   awaiting-operator filter (no UI change).
4. A ``follow_up_draft`` row goes into the operator journal so the
   operator sees the linkage between the parent advisor and the new
   draft in the journal pane.
5. Accepting the gate routes the draft to ``AGENT_CONTROLLER`` (if the
   controller daemon is live) or ``CODER_SEAT`` via the kernel's
   existing ``_kind_for_item`` helper. Either path allocates a worktree
   at dispatch time because the draft carries ``OPERATOR_GATE``
   risk_class (see ``worktree.requires_worktree``).

The mapper never raises into ``_reconcile_agenda_for_worker``: any
internal error is logged and swallowed because journal availability +
agenda hygiene must not block worker completion.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    ApprovalGate,
    mint_agenda_id,
)
from tesseract.orchestrator.autonomy.text_quality import (
    actionable_goal as _actionable_goal,
    first_sentence as _first_sentence,
)
from tesseract.orchestrator.workers.record import RiskClass, WorkerRecord, WorkerStatus

log = logging.getLogger(__name__)


_DEFAULT_KEYWORDS: tuple[str, ...] = (
    "implement",
    "create",
    "add",
    "fix",
    "refactor",
    "migrate",
    "extract",
    "rename",
    "update",
    "patch",
    "rewrite",
)
_DEFAULT_MIN_CHARS = 200


def _slug_from_goal(goal: str, *, cap: int = 32) -> str:
    """Kebab-cased ``mint_agenda_id`` slug — first ``cap`` chars after
    folding whitespace + dropping non-alphanum-or-dash chars."""
    folded = re.sub(r"[^a-zA-Z0-9]+", "-", goal.lower()).strip("-")
    if not folded:
        folded = "follow-up"
    return folded[:cap].rstrip("-") or "follow-up"


@dataclass(frozen=True)
class FollowUpConfig:
    """Resolved from ``agenda.yaml::follow_up_mapper`` at kernel boot.

    Frozen so a watcher reload swaps the whole dataclass rather than
    mutating fields under a tick.
    """

    enabled: bool = True
    min_summary_chars: int = _DEFAULT_MIN_CHARS
    keywords: tuple[str, ...] = _DEFAULT_KEYWORDS

    @classmethod
    def from_yaml_block(cls, block: dict[str, Any] | None) -> "FollowUpConfig":
        """Tolerant loader — missing keys fall through to defaults. A
        non-boolean ``enabled`` flag, non-int ``min_summary_chars``, or
        non-list ``actionable_keywords`` logs a warning and uses the
        default rather than raising at boot."""
        if not block:
            return cls()
        enabled = block.get("enabled", True)
        if not isinstance(enabled, bool):
            log.warning(
                "follow_up_mapper: agenda.yaml::follow_up_mapper.enabled "
                "must be bool, got %r — falling back to True",
                enabled,
            )
            enabled = True
        raw_min = block.get("min_summary_chars", _DEFAULT_MIN_CHARS)
        try:
            min_chars = int(raw_min)
            if min_chars < 0:
                raise ValueError("must be non-negative")
        except (TypeError, ValueError) as exc:
            log.warning(
                "follow_up_mapper: invalid min_summary_chars %r (%s); "
                "falling back to %d",
                raw_min, exc, _DEFAULT_MIN_CHARS,
            )
            min_chars = _DEFAULT_MIN_CHARS
        raw_keywords = block.get("actionable_keywords")
        if raw_keywords is None:
            keywords: tuple[str, ...] = _DEFAULT_KEYWORDS
        elif isinstance(raw_keywords, list):
            cleaned = tuple(
                str(k).strip().lower() for k in raw_keywords if str(k).strip()
            )
            keywords = cleaned or _DEFAULT_KEYWORDS
        else:
            log.warning(
                "follow_up_mapper: actionable_keywords must be a list, got %r; "
                "falling back to defaults",
                raw_keywords,
            )
            keywords = _DEFAULT_KEYWORDS
        return cls(enabled=enabled, min_summary_chars=min_chars, keywords=keywords)


class FollowUpMapper:
    """Post-completion mapper that drafts a follow-up agenda item from a
    non-trivial advisor summary.

    The kernel owns the call site; the mapper owns the policy. Two
    inputs at construction — :class:`AgendaStore` and
    :class:`FollowUpConfig` — so tests can drive the mapper with a
    tmp_path store + arbitrary config.
    """

    def __init__(
        self,
        agenda_store: AgendaStore,
        config: FollowUpConfig | None = None,
    ) -> None:
        self._agenda = agenda_store
        self._config = config or FollowUpConfig()

    @property
    def config(self) -> FollowUpConfig:
        return self._config

    def is_actionable(self, record: WorkerRecord) -> bool:
        """``True`` when the advisor summary clears length floor + at
        least one keyword. Anything else (no summary, short summary,
        keyword miss, mapper disabled) → False."""
        if not self._config.enabled:
            return False
        if record.status is not WorkerStatus.DONE:
            return False
        summary = (record.summary or "").strip()
        if len(summary) < self._config.min_summary_chars:
            return False
        artifacts_count = len(record.artifacts or [])
        if artifacts_count != 0:
            # Already produced work — no follow-up needed.
            return False
        lowered = summary.lower()
        if not any(keyword in lowered for keyword in self._config.keywords):
            return False
        return bool(_actionable_goal(summary, self._config.keywords))

    def create_draft(
        self, record: WorkerRecord, *, now: datetime | None = None
    ) -> AgendaItem | None:
        """Mint and persist a draft :class:`AgendaItem` linked to
        ``record``. Returns the item, or ``None`` if persistence fails
        (the parent path must keep going — a missed draft is recoverable
        from the journal; an unhandled raise here would taint the
        worker reconcile)."""
        when = now or datetime.now(timezone.utc)
        summary = (record.summary or "").strip()
        title = _actionable_goal(summary, self._config.keywords) or _first_sentence(summary)
        goal = title or "Follow-up from advisor"
        slug = _slug_from_goal(goal)
        item_id = mint_agenda_id(slug, now=when)
        # Idempotency — a reconcile retry within the same UTC minute
        # would mint the same id and `AgendaStore.add` would raise
        # ValueError. Pre-check so a retry is a quiet no-op rather than
        # a noisy traceback log.
        existing = self._agenda.get(item_id)
        if existing is not None:
            log.debug(
                "follow_up_mapper: draft %s already exists; skip "
                "(record=%s)",
                item_id,
                record.id,
            )
            return existing
        gate = ApprovalGate(kind="operator_review", target=item_id)
        item = AgendaItem(
            id=item_id,
            created_at=when,
            updated_at=when,
            source=AgendaSource.SELF_REFLECTION,
            source_event_id=record.id,
            goal=goal,
            rationale=(
                f"Auto-drafted follow-up from advisor worker {record.id}. "
                f"Summary: {summary[:1500]}"
            ),
            risk_class=RiskClass.OPERATOR_GATE,
            approvals_required=[gate],
            status=AgendaStatus.AWAITING_OPERATOR,
            linked_workers=[record.id],
        )
        try:
            self._agenda.add(item, by="kernel", reason="follow_up_draft")
        except ValueError as exc:
            # Race: another reconcile minted the same id between our
            # `get` and `add`. Treat as the idempotent no-op above.
            log.debug(
                "follow_up_mapper: draft %s collision (%s); skip",
                item_id, exc,
            )
            return self._agenda.get(item_id)
        except Exception:  # noqa: BLE001 — must not poison reconcile
            log.exception(
                "follow_up_mapper: failed to persist draft for worker %s",
                record.id,
            )
            return None
        return item

    def create_draft_if_actionable(
        self, record: WorkerRecord
    ) -> AgendaItem | None:
        """Kernel-facing entry point — gates on :meth:`is_actionable`,
        creates the draft, writes the ``follow_up_draft`` journal row.

        Returns the new :class:`AgendaItem` on success, ``None``
        otherwise. Never raises — every failure path logs and returns
        ``None``."""
        try:
            if not self.is_actionable(record):
                return None
            draft = self.create_draft(record)
            if draft is None:
                return None
        except Exception:  # noqa: BLE001
            log.exception(
                "follow_up_mapper: create_draft_if_actionable raised "
                "for worker %s",
                record.id,
            )
            return None
        try:
            operator_journal.append(
                "follow_up_draft",
                {
                    "agenda_item_id": draft.id,
                    "worker_id": record.id,
                    "follow_up_draft_id": draft.id,
                    "summary": draft.goal,
                },
            )
        except Exception:  # noqa: BLE001 — journal is best-effort
            log.debug(
                "follow_up_mapper: journal append failed for draft %s",
                draft.id,
                exc_info=True,
            )
        return draft


__all__ = [
    "FollowUpConfig",
    "FollowUpMapper",
]
