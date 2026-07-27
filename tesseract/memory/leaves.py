"""AU-16 S1 — leaf stream + five-state machine + atomic IO.

A ``MemoryLeaf`` is the raw input to the memory pipeline — one chat turn,
one agent observation, one channel inbound, one observer hint. Leaves
flow through a five-state machine driven by three ``BaseJob`` subclasses
(``ExtractChunkJob`` / ``AppendBufferJob`` / ``SealJob``):

    pending_extraction ──┬─→ admitted ─→ buffered ─→ sealed (terminal)
                         └─→ dropped (terminal)

Storage layout, mirroring the AU-3 worker + AU-4 agenda substrate:

    <TESSERACT_HOME>/memory-store/leaves/
        active/<leaf_id>.json          # non-terminal
        archive/<YYYY-MM>/<leaf_id>.json   # SEALED / DROPPED

Atomic writes via ``<pid>.<6hex>.tmp`` + ``os.replace`` keep concurrent
writers (job loop + recovery scan) from tearing files.

This module ships the schema + store CRUD only. The trees consuming
sealed leaves land in S2; the Obsidian round-trip mirror lands in S3.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------


class LeafState(str, Enum):
    PENDING_EXTRACTION = "pending_extraction"
    ADMITTED = "admitted"
    DROPPED = "dropped"
    BUFFERED = "buffered"
    SEALED = "sealed"


TERMINAL_STATES: frozenset[LeafState] = frozenset(
    {LeafState.DROPPED, LeafState.SEALED}
)


# Allowed transitions. A leaf MUST flow forward through this map; any
# off-graph transition raises so an upstream bug surfaces immediately
# instead of silently quarantining a leaf in an impossible state.
_ALLOWED_TRANSITIONS: dict[LeafState, frozenset[LeafState]] = {
    LeafState.PENDING_EXTRACTION: frozenset(
        {LeafState.ADMITTED, LeafState.DROPPED}
    ),
    LeafState.ADMITTED: frozenset({LeafState.BUFFERED, LeafState.DROPPED}),
    LeafState.BUFFERED: frozenset({LeafState.SEALED, LeafState.DROPPED}),
    LeafState.DROPPED: frozenset(),
    LeafState.SEALED: frozenset(),
}


class LeafTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_state: LeafState | None
    to_state: LeafState
    at: datetime
    reason: str = ""


class MemoryLeaf(BaseModel):
    """Single raw memory candidate. Mutates in place via ``transition_to``;
    caller follows every state change with ``LeafStore.save`` for the
    rewrite to hit disk."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str = Field(min_length=1, max_length=200)
    created_at: datetime
    updated_at: datetime
    state: LeafState = LeafState.PENDING_EXTRACTION
    state_history: list[LeafTransition] = Field(default_factory=list)

    body: str = Field(default="", max_length=20000)
    title: str = Field(default="", max_length=200)
    entities: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)
    drop_reason: str | None = None
    # Optional pointer set by SealJob — the id of the memory record (or
    # tree-summary file in S2) the leaf rolled into.
    sealed_into: str | None = None

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition_to(
        self,
        new_state: LeafState,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> None:
        """Append a transition entry, bump ``updated_at``, set ``state``.
        Raises ``ValueError`` on an off-graph transition."""
        if new_state == self.state:
            return
        allowed = _ALLOWED_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise ValueError(
                f"leaf {self.id}: cannot transition {self.state.value} → {new_state.value}"
            )
        when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.state_history.append(
            LeafTransition(
                from_state=self.state,
                to_state=new_state,
                at=when,
                reason=reason,
            )
        )
        self.state = new_state
        self.updated_at = when
        if new_state is LeafState.DROPPED and reason:
            self.drop_reason = reason[:200]


# ---------------------------------------------------------------------
# Identity + path helpers (call-time TESSERACT_HOME)
# ---------------------------------------------------------------------


def mint_leaf_id() -> str:
    """Return ``leaf_<8hex>``. Collision-resistant under realistic load."""
    return f"leaf_{secrets.token_hex(4)}"


def _resolve_home() -> Path:
    """Read ``TESSERACT_HOME`` at call time so tests routing via
    monkeypatched env see the per-test sandbox."""
    raw = os.environ.get("TESSERACT_HOME")
    if raw:
        return Path(raw).resolve()
    from tesseract.paths import TESSERACT_DIR
    return TESSERACT_DIR


def leaves_root() -> Path:
    return _resolve_home() / "memory-store" / "leaves"


def leaves_active_dir() -> Path:
    return leaves_root() / "active"


def leaves_archive_dir() -> Path:
    return leaves_root() / "archive"


def leaf_active_path(leaf_id: str) -> Path:
    return leaves_active_dir() / f"{leaf_id}.json"


def leaf_archive_path(leaf_id: str, *, when: datetime) -> Path:
    bucket = when.astimezone(timezone.utc).strftime("%Y-%m")
    return leaves_archive_dir() / bucket / f"{leaf_id}.json"


# ---------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------


_LEAF_ID_RE = re.compile(r"^leaf_[a-f0-9]{8}$")


class LeafStore:
    """Per-leaf JSON CRUD with atomic writes and terminal-state archive.

    Mirrors the AU-4 ``AgendaStore`` pattern: active records live in
    ``active/<id>.json``; on transition to a terminal state, the file
    moves to ``archive/<YYYY-MM>/<id>.json`` (``YYYY-MM`` is the
    ``updated_at`` month, not the id-encoded creation date — leaves
    sealed weeks after creation belong in the seal-month bucket).
    """

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = (root or leaves_root()).resolve()
        self._active = self._root / "active"
        self._archive = self._root / "archive"
        self._active.mkdir(parents=True, exist_ok=True)
        self._archive.mkdir(parents=True, exist_ok=True)

    # ---- internal ----

    def _atomic_write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
        )
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8"
        )
        os.replace(tmp, path)

    def _read_path(self, path: Path) -> MemoryLeaf | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log.warning("leaf store: unreadable file %s", path)
            return None
        try:
            return MemoryLeaf.model_validate_json(raw)
        except Exception:
            log.warning("leaf store: malformed leaf %s", path, exc_info=True)
            return None

    def _find_in_archive(self, leaf_id: str) -> Path | None:
        if not self._archive.exists():
            return None
        for bucket in self._archive.iterdir():
            if not bucket.is_dir():
                continue
            candidate = bucket / f"{leaf_id}.json"
            if candidate.exists():
                return candidate
        return None

    # ---- API ----

    def add(self, leaf: MemoryLeaf) -> None:
        """Persist a fresh leaf. Seeds a from_state=None transition if the
        caller didn't already record one. Refuses to overwrite an existing
        id — leaf ids are minted, never reused."""
        if not _LEAF_ID_RE.match(leaf.id):
            raise ValueError(f"leaf id must match {_LEAF_ID_RE.pattern!r}")
        path = self._active / f"{leaf.id}.json"
        if path.exists() or self._find_in_archive(leaf.id) is not None:
            raise ValueError(f"leaf {leaf.id!r} already exists")
        if not leaf.state_history:
            leaf.state_history.append(
                LeafTransition(
                    from_state=None,
                    to_state=leaf.state,
                    at=leaf.created_at,
                    reason="created",
                )
            )
        self._atomic_write(path, leaf.model_dump(mode="json"))

    def save(self, leaf: MemoryLeaf) -> None:
        """Persist a mutated leaf. Routes terminal-state writes through
        ``_archive`` so the file lands in the right bucket and the active
        copy is removed in one step."""
        if leaf.is_terminal():
            self._archive_leaf(leaf)
            return
        path = self._active / f"{leaf.id}.json"
        self._atomic_write(path, leaf.model_dump(mode="json"))

    def get(self, leaf_id: str) -> MemoryLeaf | None:
        active = self._active / f"{leaf_id}.json"
        if active.exists():
            return self._read_path(active)
        archived = self._find_in_archive(leaf_id)
        if archived is not None:
            return self._read_path(archived)
        return None

    def transition(
        self,
        leaf: MemoryLeaf,
        new_state: LeafState,
        *,
        reason: str = "",
    ) -> None:
        """Canonical state-change path: mutate in memory + persist in one
        atomic write. Terminal transitions archive in the same call."""
        leaf.transition_to(new_state, reason=reason)
        self.save(leaf)

    def iter_active(self) -> Iterator[MemoryLeaf]:
        if not self._active.exists():
            return
        for path in self._active.glob("leaf_*.json"):
            leaf = self._read_path(path)
            if leaf is not None:
                yield leaf

    def list_in_state(self, state: LeafState) -> list[MemoryLeaf]:
        return [leaf for leaf in self.iter_active() if leaf.state is state]

    def count_in_state(self, state: LeafState) -> int:
        return sum(1 for leaf in self.iter_active() if leaf.state is state)

    # ---- archive ----

    def _archive_leaf(self, leaf: MemoryLeaf) -> None:
        target = self._archive / leaf.updated_at.astimezone(timezone.utc).strftime(
            "%Y-%m"
        ) / f"{leaf.id}.json"
        self._atomic_write(target, leaf.model_dump(mode="json"))
        active = self._active / f"{leaf.id}.json"
        try:
            active.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "LeafState",
    "LeafStore",
    "LeafTransition",
    "MemoryLeaf",
    "TERMINAL_STATES",
    "leaf_active_path",
    "leaf_archive_path",
    "leaves_active_dir",
    "leaves_archive_dir",
    "leaves_root",
    "mint_leaf_id",
]
