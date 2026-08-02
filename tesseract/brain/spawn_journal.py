"""Best-effort spawn start/terminal journal (P6 Task 3 §G5).

The restored-history-scan alternative is not viable: ``chat_store.py`` persists
role/content history only, and spawn completions are one-shot injections
spliced into the adapter message list — never appended to ``ChatSession.
history`` (``chat.py::_messages_for_turn``) — so there is nothing
machine-recoverable to scan after a restart.

Instead, ``SpawnRegistry`` appends a ``start`` event when a spawn is
registered and a ``terminal`` event from its done-callback, one line per
event, to ``<TESSERACT_HOME>/logs/sessions/<session_id>/spawns.jsonl``. On
resume (page reload / backend restart rebuilding a chat from its persisted
``ChatRecord``, or an archived chat restored from a prior session), the
owning session_id's journal is swept for orphans — a ``start`` with no
matching ``terminal`` — which are surfaced as one-shot ``[spawn_lost]``
notes and immediately marked terminal so a second sweep of the same journal
can't re-report them. No cross-restart resumption: vanished means failed.

Writes are best-effort: an IO error here must never block or fail spawn
execution (same discipline as memory writes) — every write is wrapped and
never raises.

Synthetic forks (``chat.py::fork_for_synthetic``) share the parent chat's
``session_id`` by design, so their spawns journal into the same parent
``spawns.jsonl`` rather than a fork-private file. This is safe: handle ids
are minted unique per spawn (never derived from session_id), so start/
terminal records from a fork and its parent never collide in the journal.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.paths import TESSERACT_HOME, log_dir

logger = logging.getLogger(__name__)


def spawn_journal_path(session_id: str) -> Path:
    """Resolve the journal path at call time (never an import-time constant)
    so a test's ``monkeypatch.setenv("TESSERACT_HOME", tmp_path)`` lands the
    JSONL under its own tmp dir (``kernel/workspace_changes.py::
    workspace_events_dir`` idiom)."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return log_dir("sessions") / session_id / "spawns.jsonl"


def _append(session_id: str, entry: dict[str, Any]) -> None:
    try:
        path = spawn_journal_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 — journal writes are best-effort
        logger.warning(
            "spawn journal append failed for session %s", session_id, exc_info=True
        )


def record_start(session_id: str, handle_id: str, kind: str, started_at: str) -> None:
    """Append a ``start`` event. No-op (silently) on any write failure."""
    _append(
        session_id,
        {"event": "start", "handle_id": handle_id, "kind": kind, "started_at": started_at},
    )


def record_terminal(session_id: str, handle_id: str, outcome: str) -> None:
    """Append a ``terminal`` event. No-op (silently) on any write failure."""
    _append(
        session_id,
        {
            "event": "terminal",
            "handle_id": handle_id,
            "outcome": outcome,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def record_parked(session_id: str, handle_id: str) -> None:
    """Append a ``parked`` event (trio W4 ask-instead-of-die parked an ASK
    for this spawn). Lets ``sweep_orphans`` tell a restart-orphaned spawn
    that died waiting on operator input apart from one that simply vanished.
    No-op (silently) on any write failure."""
    _append(
        session_id,
        {
            "event": "parked",
            "handle_id": handle_id,
            "parked_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _is_live(handle_id: str) -> bool:
    """True when the process-global handle registry still holds a
    non-terminal handle for this id (Task 3.1) — proof the spawn is still
    running, or PARKED on an operator ask (trio W4's ``input_required``,
    which is alive, not lost), in THIS process. A same-process reconnect
    (page reload / resume against a backend that never restarted) must not
    declare such a spawn lost just because it hasn't written its own
    ``terminal`` journal line yet.

    Lazy import: mirrors ``spawns.py``'s own lazy import of this module,
    avoiding a module-level circular import between the two.
    """
    from tesseract.brain.spawns import find_handle

    handle = find_handle(handle_id)
    return handle is not None and handle.is_running()


def sweep_orphans(session_id: str) -> list[dict[str, str]]:
    """Return ``start`` records with no matching ``terminal`` event AND no
    still-live handle in this process, and mark them ``terminal`` (outcome
    ``"vanished"``) so a repeat sweep of the same journal (e.g. a second
    restore of the same persisted chat) can't re-report them.

    A ``start`` whose handle ``_is_live`` (Task 3.1) is skipped entirely —
    it is a same-process reconnect racing its own still-running spawn, not a
    genuine orphan. It's left untouched in the journal; its real
    ``terminal`` event will land normally once the spawn actually finishes.

    ``""``/missing ``session_id`` (an old ``ChatRecord`` predating the
    ``session_id`` field) and a missing/corrupt journal both yield ``[]``
    rather than raising — resume must never fail because of this.
    """
    if not session_id:
        return []
    path = spawn_journal_path(session_id)
    if not path.exists():
        return []
    starts: dict[str, dict[str, str]] = {}
    terminated: set[str] = set()
    parked: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                handle_id = rec.get("handle_id")
                if not handle_id:
                    continue
                if rec.get("event") == "start":
                    starts[handle_id] = rec
                elif rec.get("event") == "terminal":
                    terminated.add(handle_id)
                elif rec.get("event") == "parked":
                    parked.add(handle_id)
    except OSError:
        logger.warning("spawn journal read failed for session %s", session_id, exc_info=True)
        return []
    orphans = [
        {**rec, "was_parked": rec["handle_id"] in parked}
        for handle_id, rec in starts.items()
        if handle_id not in terminated and not _is_live(handle_id)
    ]
    for rec in orphans:
        record_terminal(session_id, rec["handle_id"], "vanished")
    return orphans


__all__ = [
    "spawn_journal_path",
    "record_start",
    "record_terminal",
    "record_parked",
    "sweep_orphans",
]
