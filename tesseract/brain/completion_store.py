"""Durable record of a finished background spawn, keyed by the chat that owns it.

Everything else about completion delivery is in-memory. The queue on
``ChatSession``, the ownership index that re-points a notifier across a
reconnect, the dead-window replay — all of it dies with the process. A spawn
that finishes seconds before the backend restarts writes a ``terminal`` line to
the spawn journal, so ``spawn_journal.sweep_orphans`` correctly declines to call
it lost, and then nothing else ever mentions it. The result is gone and no
signal survives to say it existed.

So the result itself is written down, in full, before the notifier fires. A
record is claimed only when the turn that drained its note commits — anything
else (cancelled, adapter error, the process taken down mid-stream) redelivers.
The file is removed once nothing in it is outstanding, so steady state is empty.

Keyed by ``chat_id`` rather than the Mirror session id: a session id changes on
every reconnect, and several chats share one, so a session-keyed record cannot
say which chat is owed the result. ``chat_id`` is stable for the life of the
chat and is what the restore path already has in hand.

Writes are best-effort in the sense that they never propagate — a spawn must
not fail because the disk did — but a failure here is a lost result, not a lost
reflection, so it is logged at ERROR rather than swallowed quietly.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Permissive enough for the uuid4 hex the Mirror mints and for readable ids in
# tests, strict enough that the id can only ever name a file inside the store.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class CompletionRecord:
    """What a finished spawn produced, in a form that outlives its handle.

    ``output`` is the untrimmed result — the compression that shapes a delivery
    block is applied when the block is rendered, so a record replayed after a
    restart compresses exactly like one delivered live. It is stored whole
    because after the restart this is the only copy: ``spawn_await`` has no
    handle left to read.
    """

    handle_id: str
    kind: str
    status: str
    output: str
    started_at: str = ""
    finished_at: str = ""
    goal: str | None = None
    session_id: str = ""
    owner_principal: str = ""
    schema: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompletionRecord":
        return cls(
            handle_id=str(data.get("handle_id", "")),
            kind=str(data.get("kind", "")),
            status=str(data.get("status", "unknown")),
            output=str(data.get("output", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            goal=data.get("goal"),
            session_id=str(data.get("session_id", "")),
            owner_principal=str(data.get("owner_principal", "")),
            schema=int(data.get("schema", SCHEMA_VERSION)),
        )


def completions_dir() -> Path:
    """``<TESSERACT_HOME>/sessions/completions``, resolved at call time so a
    test's ``TESSERACT_HOME`` override lands the store under its own tmp dir
    (``kernel/workspace_changes.py::workspace_events_dir`` idiom). Sits beside
    ``sessions/chats`` because a completion is chat state, not a log."""
    override = os.environ.get("TESSERACT_HOME")
    if override:
        base = Path(override).resolve()
    else:
        from tesseract.paths import TESSERACT_HOME

        base = TESSERACT_HOME
    return base / "sessions" / "completions"


def _path(chat_id: str) -> Path:
    if not _CHAT_ID_RE.fullmatch(chat_id or ""):
        raise ValueError(f"invalid chat_id for the completion store: {chat_id!r}")
    return completions_dir() / f"{chat_id}.jsonl"


def result_text(handle: Any) -> str:
    """Everything the spawn produced, as text. A raised exception IS the
    result for a failed spawn — reported, never swallowed."""
    try:
        if handle.task.cancelled() or handle.cancelled:
            return "(cancelled)"
        exc = handle.task.exception()
        if exc is not None:
            return f"{type(exc).__name__}: {exc}"
        result = handle.task.result()
        return (getattr(result, "output", "") or "").strip() or "(no output)"
    except Exception:  # noqa: BLE001 — a malformed handle still gets delivered
        return "(result unavailable)"


def record_from_handle(
    handle: Any, *, session_id: str = "", owner_principal: str = ""
) -> CompletionRecord:
    """Snapshot a handle into a record. Tolerant by construction: the same
    shape is used to render a live delivery, and a handle that cannot answer
    for itself must still deliver something rather than raise inside a task
    done-callback."""
    try:
        status = handle.status()
    except Exception:  # noqa: BLE001
        status = "unknown"
    return CompletionRecord(
        handle_id=str(getattr(handle, "handle_id", "") or ""),
        kind=str(getattr(handle, "kind", "") or ""),
        status=status,
        output=result_text(handle),
        started_at=str(getattr(handle, "started_at", "") or ""),
        finished_at=str(getattr(handle, "finished_at", "") or ""),
        goal=getattr(handle, "goal", None),
        session_id=session_id,
        owner_principal=owner_principal,
    )


def record(chat_id: str, rec: CompletionRecord) -> None:
    """Append a completion. Never raises — a spawn's done-callback must not
    die on a disk error — but logs at ERROR, because what was lost is a
    result."""
    try:
        path = _path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "completed", **asdict(rec)}) + "\n")
    except Exception:  # noqa: BLE001
        logger.error(
            "completion store: %s could not be recorded for chat %s — it will "
            "not survive a restart",
            rec.handle_id,
            chat_id,
            exc_info=True,
        )


def _read(chat_id: str) -> tuple[dict[str, CompletionRecord], set[str]]:
    path = _path(chat_id)
    records: dict[str, CompletionRecord] = {}
    delivered: set[str] = set()
    if not path.exists():
        return records, delivered
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                handle_id = entry.get("handle_id")
                if not handle_id:
                    continue
                if entry.get("event") == "completed":
                    records[handle_id] = CompletionRecord.from_dict(entry)
                elif entry.get("event") == "delivered":
                    delivered.add(handle_id)
    except OSError:
        logger.warning(
            "completion store: read failed for chat %s", chat_id, exc_info=True
        )
        return {}, set()
    return records, delivered


def pending(chat_id: str) -> list[CompletionRecord]:
    """Recorded completions this chat has not yet been told about, oldest
    first. A read failure yields nothing rather than raising — a restore must
    never fail because of this."""
    records, delivered = _read(chat_id)
    return [rec for hid, rec in records.items() if hid not in delivered]


def mark_delivered(chat_id: str, handle_ids: list[str]) -> None:
    """Claim completions once the turn that drained their notes has committed.

    Appended rather than rewritten so a crash mid-claim leaves the record
    outstanding — redelivering a result the model may already have seen is
    recoverable; dropping one is not. Once nothing in the file is outstanding
    the file goes, so a long-lived chat does not accumulate a claim log.
    """
    if not handle_ids:
        return
    try:
        path = _path(chat_id)
        if not path.exists():
            return
        with path.open("a", encoding="utf-8") as f:
            for handle_id in handle_ids:
                f.write(json.dumps({"event": "delivered", "handle_id": handle_id}) + "\n")
        if not pending(chat_id):
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — this runs inside a turn's drain
        logger.warning(
            "completion store: could not claim %d completion(s) for chat %s — "
            "they may be redelivered",
            len(handle_ids),
            chat_id,
            exc_info=True,
        )


def discard(chat_id: str) -> None:
    """Drop every outstanding record for a chat — used when the operator wipes
    the chat, where replaying afterwards would undo them."""
    try:
        _path(chat_id).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — `/reset` must not fail on this
        logger.warning(
            "completion store: discard failed for chat %s", chat_id, exc_info=True
        )


__all__ = [
    "CompletionRecord",
    "completions_dir",
    "discard",
    "mark_delivered",
    "pending",
    "record",
    "record_from_handle",
    "result_text",
]
