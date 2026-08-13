"""AgendaStore — atomic per-file JSON + append-only ``index.jsonl``.

File layout (under ``<TESSERACT_HOME>/agenda/``):

    active/<id>.json         — items not yet terminal
    archive/YYYY-MM/<id>.json — terminal items, bucketed by updated_at
    index.jsonl              — one row per status transition (audit trail)

Atomic write per record: ``<pid>.<6hex>.tmp`` + ``os.replace`` (same
volume → atomic on Windows + POSIX). Per-writer tmp suffix prevents two
concurrent writers from interleaving over a shared temp file —
mirrors the AU-3 ``workers/record.py::_atomic_write_json`` pattern after
the reviewer flagged the race there.

``index.jsonl`` is intentionally not the source of truth — recovery
reads ``active/*.json``, the index is the audit log. A missing index row
is recoverable; a corrupt item file surfaces via Pydantic at load.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from pydantic import ValidationError

from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    StatusTransition,
    TERMINAL_STATUSES,
    TransitionActor,
    dedupe_key,
)

AgendaBroadcastHook = Callable[[str, AgendaItem, Mapping[str, Any]], None]
from tesseract.orchestrator.autonomy.paths import (
    agenda_active_dir,
    agenda_archive_dir,
    agenda_archive_path,
    agenda_index_path,
    agenda_item_path,
    agenda_root,
)
from tesseract.orchestrator.autonomy.scoring import AgendaWeights, score_item
from tesseract.orchestrator.workers.record import RiskClass

log = logging.getLogger(__name__)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` atomically via a per-process tmp file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _append_index(row: dict[str, Any]) -> None:
    """Append a single JSON line to ``index.jsonl``. Best-effort: write failures log and fall through."""
    path = agenda_index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        log.exception("agenda: index.jsonl append failed")


class AgendaStore:
    """CRUD over per-file agenda items. Construct once per backend.

    The store keeps no in-memory cache — every read scans disk. Volume
    is small (Phase 1 ceiling: a few hundred active items) and re-scan
    is simpler than cache coherence. AU-5 may add a cache later if
    profiling shows the scan cost is real.

    Construction takes no arguments by design: every path resolves
    against ``TESSERACT_HOME`` at call time so test fixtures that
    ``monkeypatch.setenv("TESSERACT_HOME", tmp_path)`` route writes
    cleanly without per-store overrides.
    """

    def __init__(
        self,
        *,
        weights: AgendaWeights | None = None,
        broadcast_hook: AgendaBroadcastHook | None = None,
    ) -> None:
        self._weights = weights or AgendaWeights()
        self._broadcast_hook = broadcast_hook

    @property
    def weights(self) -> AgendaWeights:
        return self._weights

    def set_weights(self, weights: AgendaWeights) -> None:
        """Replace the scoring weights. Existing items keep their cached
        ``priority_score`` until the next ``recompute_score`` call."""
        self._weights = weights

    def set_broadcast_hook(self, hook: AgendaBroadcastHook | None) -> None:
        """Wire an optional sync broadcaster fired on every mutation that
        completes a disk write. The hook receives ``(event_type, item, extras)``
        and is expected to schedule any async fan-out itself (the store stays
        sync so the kernel's `transition` call sites don't need rewriting).

        Mirror server boot uses this to fan WS envelopes from kernel-internal
        mutations that bypass ``routes/agenda.py``. Hook exceptions are
        swallowed — broadcast failures must not affect the agenda mutation.
        """
        self._broadcast_hook = hook

    def _fire_broadcast(
        self,
        event_type: str,
        item: AgendaItem,
        extras: Mapping[str, Any] | None = None,
    ) -> None:
        if self._broadcast_hook is None:
            return
        try:
            self._broadcast_hook(event_type, item, extras or {})
        except Exception:
            log.exception(
                "agenda broadcast hook raised on %s for item %s; non-fatal",
                event_type, item.id,
            )

    # -- CRUD ----------------------------------------------------------

    def add(
        self,
        item: AgendaItem,
        *,
        by: TransitionActor = "kernel",
        reason: str = "created",
    ) -> AgendaItem:
        """Persist a new item to ``active/``. Records the initial
        ``proposed`` transition in both the item's history and the index
        log. Refuses ``absolute_deny`` items at admission per
        ``_shared/risk-class-taxonomy.md``."""
        if item.risk_class == RiskClass.ABSOLUTE_DENY:
            raise ValueError(
                f"agenda: refuse to admit absolute_deny item {item.id!r}"
            )
        existing_path = agenda_item_path(item.id)
        if existing_path.exists():
            raise ValueError(f"agenda: item {item.id!r} already exists")

        # Initial history entry — None → current status. ``transition_to``
        # short-circuits same-status calls, so we synthesize the row.
        if not item.status_history:
            item.status_history.append(
                StatusTransition(
                    from_status=None,
                    to_status=item.status,
                    at=item.created_at,
                    reason=reason,
                    by=by,
                )
            )

        self.recompute_score(item)
        _atomic_write_json(existing_path, item.model_dump(mode="json"))
        _append_index(
            {
                "id": item.id,
                "ts": item.created_at.isoformat(),
                "event": "created",
                "from": None,
                "to": item.status.value,
                "by": by,
                "reason": reason,
                "source": item.source.value,
                "risk_class": item.risk_class.value,
            }
        )
        self._fire_broadcast("agenda_item_added", item)
        return item

    def get(self, item_id: str) -> AgendaItem | None:
        """Read by id. Searches ``active/`` first, then archive buckets.
        Returns None if absent or if the id could not name a file here;
        raises ValidationError on a malformed record so corruption surfaces
        at the recovery boundary."""
        try:
            path = agenda_item_path(item_id)
        except ValueError:
            return None
        if not path.exists():
            path = self._find_in_archive(item_id)
            if path is None:
                return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return AgendaItem.model_validate(raw)

    def save(self, item: AgendaItem) -> Path:
        """Atomic rewrite; recomputes score before writing. Terminal items route
        to ``_archive``. Returns the resolved on-disk path."""
        self.recompute_score(item)
        if item.is_terminal():
            return self._archive(item)
        path = agenda_item_path(item.id)
        _atomic_write_json(path, item.model_dump(mode="json"))
        return path

    def transition(
        self,
        item: AgendaItem,
        new_status: AgendaStatus,
        *,
        reason: str = "",
        by: TransitionActor = "kernel",
    ) -> AgendaItem:
        """Convenience: ``item.transition_to`` + ``save`` + index append."""
        prior = item.status
        if new_status == prior:
            return item
        item.transition_to(new_status, reason=reason, by=by)
        self.save(item)
        _append_index(
            {
                "id": item.id,
                "ts": item.updated_at.isoformat(),
                "event": "transition",
                "from": prior.value,
                "to": new_status.value,
                "by": by,
                "reason": reason,
            }
        )
        self._fire_broadcast(
            "agenda_item_transitioned", item, {"prior_status": prior.value}
        )
        return item

    # -- listing -------------------------------------------------------

    def iter_active(self) -> Iterator[AgendaItem]:
        """Walk ``active/`` yielding well-formed records. Malformed
        files are logged and skipped — recovery surfaces them via its
        ``scan_error`` bucket."""
        root = agenda_active_dir()
        if not root.exists():
            return
        for child in sorted(root.iterdir()):
            if child.suffix != ".json":
                continue
            try:
                raw = json.loads(child.read_text(encoding="utf-8"))
                yield AgendaItem.model_validate(raw)
            except (OSError, ValueError, ValidationError) as exc:
                log.warning("agenda: skipping malformed %s: %s", child.name, exc)
                continue

    def list_active(self) -> list[AgendaItem]:
        return list(self.iter_active())

    def find_dedupe(
        self,
        goal: str,
        source: AgendaSource,
    ) -> AgendaItem | None:
        """Scan active items for a matching (goal, source) pair. Used
        by AU-5 mappers to avoid re-admitting the same observer signal
        every minute. Linear scan; volume is small enough."""
        key = dedupe_key(goal, source)
        for item in self.iter_active():
            if dedupe_key(item.goal, item.source) == key:
                return item
        return None

    def find_fuzzy_dedupe(
        self,
        goal: str,
        source: AgendaSource,
        *,
        threshold: float,
        window_hours: int,
        now: datetime | None = None,
    ) -> AgendaItem | None:
        """Near-duplicate of an existing ACTIVE item from the SAME source
        created within ``window_hours``. Similarity = difflib
        ``SequenceMatcher`` ratio on whitespace-normalised lowercased
        goals. Returns the first match at/above ``threshold``, else
        None. Exact-match dedupe stays in ``find_dedupe``; this catches
        re-phrasings (heartbeat every tick)."""
        moment = now or datetime.now(timezone.utc)
        cutoff = moment - timedelta(hours=window_hours)
        normalised = " ".join(goal.lower().split())
        for item in self.iter_active():
            if item.source != source:
                continue
            created = item.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                continue
            other = " ".join(item.goal.lower().split())
            ratio = SequenceMatcher(None, normalised, other).ratio()
            if ratio >= threshold:
                return item
        return None

    def count_open_by_source(self, source: AgendaSource) -> int:
        """Count active (non-terminal) items with this source."""
        return sum(1 for item in self.iter_active() if item.source == source)

    def count_open_total(self) -> int:
        """Count all active (non-terminal) items."""
        return sum(1 for _ in self.iter_active())

    # -- scoring -------------------------------------------------------

    def recompute_score(self, item: AgendaItem) -> None:
        """Mutate ``priority_score`` + ``score_components`` +
        ``score_computed_at`` against the current weights."""
        total, components = score_item(item, self._weights)
        item.priority_score = total
        item.score_components = components
        item.score_computed_at = datetime.now(timezone.utc)

    def ranked(self) -> list[AgendaItem]:
        """Return active items sorted by priority_score (desc) with
        ``created_at`` as the deterministic tie-breaker so identical-
        score items have a stable order across boots."""
        items = self.list_active()
        for item in items:
            self.recompute_score(item)
        items.sort(key=lambda i: (-i.priority_score, i.created_at))
        return items

    # -- budget accounting --------------------------------------------

    def today_spend(self) -> dict[str, int]:
        """Aggregate tokens + seconds spent across active items.
        Kernel hook for the daily cap check; cap enforcement itself
        lands in AU-5 / AU-6."""
        tokens = 0
        seconds = 0
        for item in self.iter_active():
            tokens += item.budget_tokens_spent
            seconds += item.budget_seconds_spent
        return {"tokens": tokens, "seconds": seconds}

    # -- archive -------------------------------------------------------

    def _archive(self, item: AgendaItem) -> Path:
        """Move a terminal item from ``active/<id>.json`` to
        ``archive/<YYYY-MM>/<id>.json``. Idempotent: if active is gone,
        return the archive path (existing or would-be)."""
        if not item.is_terminal():
            raise ValueError(
                f"agenda: refuse to archive non-terminal item {item.id!r} "
                f"({item.status.value})"
            )
        month = item.updated_at.astimezone(timezone.utc).strftime("%Y-%m")
        dst = agenda_archive_path(item.id, month)
        dst.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(dst, item.model_dump(mode="json"))
        # Remove the active copy after the archive write commits.
        active = agenda_item_path(item.id)
        try:
            active.unlink(missing_ok=True)
        except OSError:
            log.exception("agenda: failed to remove active copy of %s", item.id)
        return dst

    def _find_in_archive(self, item_id: str) -> Path | None:
        """Walk every ``archive/YYYY-MM/`` bucket. We can't probe a
        single bucket directly: the archive is keyed by ``updated_at``
        (the terminal-transition time), not by the creation date the
        id encodes — an item created late on the last day of a month
        and archived a day later lives in a different bucket than the
        id suggests. Archive volume is small enough that a full walk
        is cheap; the previous fast-path optimisation silently missed
        cross-month items and relied on the fallback scan anyway."""
        root = agenda_archive_dir()
        if not root.exists():
            return None
        for month_dir in root.iterdir():
            if not month_dir.is_dir():
                continue
            candidate = month_dir / f"{item_id}.json"
            if candidate.exists():
                return candidate
        return None


def load_weights_from_yaml(path: Path) -> AgendaWeights:
    """Build an ``AgendaWeights`` from a YAML file shaped like
    ``tesseract/config/agenda.yaml``. Missing keys fall back to the
    dataclass defaults — operator can omit any tunable."""
    import yaml  # local import: agenda config is optional in test fixtures

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scoring_raw = raw.get("scoring") or {}
    age_raw = raw.get("age_score") or {}
    trust_raw = raw.get("source_trust") or {}

    defaults = AgendaWeights()
    trust_map = dict(defaults.source_trust)
    for src_name, val in trust_raw.items():
        try:
            trust_map[AgendaSource(src_name)] = float(val)
        except (ValueError, TypeError):
            log.warning("agenda.yaml: ignoring unknown source_trust key %r", src_name)

    return AgendaWeights(
        operator_priority_weight=float(
            scoring_raw.get("operator_priority_weight", defaults.operator_priority_weight)
        ),
        age_weight=float(scoring_raw.get("age_weight", defaults.age_weight)),
        risk_weight=float(scoring_raw.get("risk_weight", defaults.risk_weight)),
        budget_remaining_weight=float(
            scoring_raw.get("budget_remaining_weight", defaults.budget_remaining_weight)
        ),
        source_trust_weight=float(
            scoring_raw.get("source_trust_weight", defaults.source_trust_weight)
        ),
        vet_weight=float(scoring_raw.get("vet_weight", defaults.vet_weight)),
        age_cap_hours=float(age_raw.get("cap", defaults.age_cap_hours)),
        source_trust=trust_map,
    )


__all__ = [
    "AgendaStore",
    "agenda_root",
    "load_weights_from_yaml",
]
