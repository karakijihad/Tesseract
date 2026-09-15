"""One Mirror-side watcher: the only producer of a task's phone message and
of the Day room's live refresh, from whichever process closed the task or
pressed the key.

Before this, a close notified the operator through an in-process callback
(`TaskCloseTool.on_closed`, wired only inside the Mirror process's own tool
registry) and a key reached the cockpit through a module-global listener
(`verdicts.set_recorded_listener`, armed only while the Mirror process was
up). Both were invisible to a named lane, a CLI session, or an MCP client: the
agent controller daemon builds its own tool registry with no `app` at all, so
a task it closed produced a row on disk and nothing else. This file replaces
both wires with one: it tails the files every closer and every key-presser
already writes to, so it does not matter which process did the writing.

Invariants, held together (a second finding on this code means stop patching
and design against the whole list, not the one finding in front of you):

1. **One path.** `TaskCloseTool.on_closed` and `verdicts.set_recorded_listener`
   are gone. A close or a key is announced exactly once, however many
   processes wrote it, because exactly one thing reads the files.
2. **The core is pure and file-based.** Given a cursor (per-file byte offsets
   plus the instant the last poll began), read new COMPLETE lines
   from `agenda/history/*.jsonl` and `agenda/verdicts/*.jsonl` and return the
   new closes, the new keys, and the advanced cursor. A partial trailing line
   with no newline yet is left for the next read. A row that fails to parse
   is skipped and counted, never raised.
3. **A close is announced only when it is recent.** `source == "task"`,
   `status` in `verdicts.JUDGED_STATUSES`, and `closed_at` at or after the
   instant the previous poll began, minus
   `agenda.yaml::verdict.watch_backfill_minutes`. The retention sweep
   (`retention/sweeps.py`) writes a history row for a task that closed long ago
   as part of its own bookkeeping; without this filter, that write would look
   exactly like a fresh close and re-announce work the operator answered a
   verdict on months ago. The anchor is the previous poll, not this session's
   start: it moves forward every poll, so a long-lived process keeps filtering,
   and it survives a restart, so a close that happened while the Mirror was
   down for any length of time is still after it.
4. **The cursor persists outside every log tree**, at
   `runtime_dir()/outcome-watch.json`, written after a poll that read a row or
   moved an offset, and otherwise at most once per `watch_max_seconds`, so an
   idle machine is not rewriting it every few seconds. The saved anchor may
   lag the one in memory by that much, which the backfill window covers. With no cursor file, a cold start begins at the CURRENT end of
   every file, so nothing already on disk is replayed. A month file this
   watcher has never seen (a new month, or a file older polls never reached)
   starts at offset 0. A restart resumes from the saved offsets, so a close
   written while the Mirror was down is still seen, subject to rule 3.
5. **Every announce is best-effort and independent.** A notify or broadcast
   failure for one row is logged and never stops the batch, and the cursor
   still advances past every row this poll read, including the one that
   failed to send: a phone message that could not go out is not a reason to
   read the same row again next poll.
6. **The loop is configured once, bounded, and fails closed on drift.**
   All three `agenda.yaml::verdict.watch_*` values are read in `start()`,
   before the background task exists, and a missing key raises out of
   `start()`, so boot records the watcher as failed rather than as running. `MAX_CONSECUTIVE_FAILURES` (3) failed polls in a row log once and
   double the interval, capped at `watch_max_seconds`, rather than spinning; a
   single success resets both the failure count and the interval.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from tesseract.orchestrator.autonomy import verdicts
from tesseract.orchestrator.autonomy.agenda_history import history_dir
from tesseract.orchestrator.autonomy.verdicts import verdicts_dir
from tesseract.paths import config_dir, runtime_dir

log = logging.getLogger(__name__)

#: Same ceiling every retry loop in this codebase holds to.
MAX_CONSECUTIVE_FAILURES = 3

_HISTORY_KIND = "history"
_VERDICTS_KIND = "verdicts"


def cursor_path() -> Path:
    """`<runtime_dir>/outcome-watch.json` — machine-local, never a log tree."""
    return runtime_dir() / "outcome-watch.json"


def _agenda_config() -> dict[str, Any]:
    import yaml

    path = config_dir() / "agenda.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _verdict_block() -> dict[str, Any]:
    raw = _agenda_config()
    block = raw.get("verdict")
    if not isinstance(block, dict):
        raise KeyError("agenda.yaml: verdict block is missing")
    return block


def watch_seconds() -> float:
    """`agenda.yaml::verdict.watch_seconds`, or a raised `KeyError`."""
    block = _verdict_block()
    if "watch_seconds" not in block:
        raise KeyError("agenda.yaml: verdict.watch_seconds is missing")
    return float(block["watch_seconds"])


def watch_backfill_minutes() -> float:
    """`agenda.yaml::verdict.watch_backfill_minutes`, or a raised `KeyError`."""
    block = _verdict_block()
    if "watch_backfill_minutes" not in block:
        raise KeyError("agenda.yaml: verdict.watch_backfill_minutes is missing")
    return float(block["watch_backfill_minutes"])


def watch_max_seconds() -> float:
    """`agenda.yaml::verdict.watch_max_seconds`, or a raised `KeyError`."""
    block = _verdict_block()
    if "watch_max_seconds" not in block:
        raise KeyError("agenda.yaml: verdict.watch_max_seconds is missing")
    return float(block["watch_max_seconds"])


@dataclass(frozen=True)
class Cursor:
    """Per-file byte offsets, plus the instant the last poll began.

    `polled_at` anchors rule 3's backfill window. Each poll replaces it with
    the instant that poll began, so it never grows stale in a long-lived
    process, and it is carried across a restart with the offsets, so a close
    written while the Mirror was down is still at or after it.
    """

    offsets: Mapping[str, int] = field(default_factory=dict)
    polled_at: str = ""

    def polled_at_dt(self) -> datetime:
        parsed = datetime.fromisoformat(self.polled_at)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def to_json(self) -> dict[str, Any]:
        return {"offsets": dict(self.offsets), "polled_at": self.polled_at}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Cursor":
        offsets = data.get("offsets")
        # `started_at` is what a cursor written by the first version of this
        # file called the same anchor.
        polled_at = data.get("polled_at") or data.get("started_at")
        return cls(
            offsets={str(k): int(v) for k, v in offsets.items()} if isinstance(offsets, dict) else {},
            polled_at=str(polled_at or ""),
        )


@dataclass(frozen=True)
class ClosedTask:
    item_id: str
    goal: str
    outcome: str  # "done" | "failed"
    verified_by: str


@dataclass(frozen=True)
class VerdictKey:
    task_id: str
    verdict: str
    by: str


@dataclass(frozen=True)
class PollResult:
    cursor: Cursor
    closes: tuple[ClosedTask, ...]
    keys: tuple[VerdictKey, ...]
    parse_errors: int


def _key_for(kind: str, path: Path) -> str:
    return f"{kind}/{path.name}"


def _read_new_lines(path: Path, start_offset: int) -> tuple[list[str], int]:
    """New complete lines from `path`, from `start_offset`. A trailing partial
    line (no final newline yet) is left unread. A missing or shrunk file is
    read from 0, since an append-only jsonl never truncates on its own."""
    if not path.is_file():
        return [], start_offset
    try:
        size = path.stat().st_size
    except OSError:
        return [], start_offset
    offset = start_offset if start_offset <= size else 0
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return [], start_offset
    if not chunk:
        return [], offset
    idx = chunk.rfind(b"\n")
    if idx == -1:
        return [], offset
    complete = chunk[:idx]
    lines = [ln.decode("utf-8", errors="replace") for ln in complete.split(b"\n") if ln]
    return lines, offset + idx + 1


def _bootstrap_offsets() -> dict[str, int]:
    """Cold start: every file that exists right now starts at its current
    end, so nothing already on disk is replayed."""
    offsets: dict[str, int] = {}
    for kind, root in ((_HISTORY_KIND, history_dir()), (_VERDICTS_KIND, verdicts_dir())):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.jsonl")):
            try:
                offsets[_key_for(kind, path)] = path.stat().st_size
            except OSError:
                continue
    return offsets


def initial_cursor(now: datetime) -> Cursor:
    return Cursor(offsets=_bootstrap_offsets(), polled_at=now.isoformat())


def load_cursor() -> Cursor | None:
    """The persisted cursor, or `None` when there is none to resume from."""
    path = cursor_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("outcome_watch: could not read the saved cursor at %s", path)
        return None
    if not isinstance(data, dict):
        return None
    return Cursor.from_json(data)


def save_cursor(cursor: Cursor) -> None:
    """Write the cursor atomically: a tmp file plus an OS-level rename, so a
    reader never sees a half-written file."""
    path = cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cursor.to_json(), separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def load_or_init_cursor(now: datetime) -> Cursor:
    """The cursor a fresh watcher session starts from: the saved offsets and
    anchor if there are any (a warm restart), the current end of every file and
    `now` otherwise (a cold start). A saved cursor with no readable anchor
    takes `now`, which is the cold-start answer for the part it lacks."""
    saved = load_cursor()
    if saved is None:
        cursor = initial_cursor(now)
    else:
        try:
            saved.polled_at_dt()
            cursor = saved
        except ValueError:
            cursor = Cursor(offsets=dict(saved.offsets), polled_at=now.isoformat())
    save_cursor(cursor)
    return cursor


def _parse_close(row: dict[str, Any], floor: datetime) -> ClosedTask | None:
    if row.get("source") != "task":
        return None
    status = row.get("status")
    if status not in verdicts.JUDGED_STATUSES:
        return None
    item_id = row.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    try:
        closed = datetime.fromisoformat(str(row.get("closed_at") or ""))
    except ValueError:
        return None
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    if closed < floor:
        return None
    return ClosedTask(
        item_id=item_id,
        goal=str(row.get("goal") or ""),
        outcome=str(status),
        verified_by=str(row.get("verification_by") or ""),
    )


def _parse_key(row: dict[str, Any]) -> VerdictKey | None:
    task_id = row.get("task")
    verdict = row.get("verdict")
    by = row.get("by")
    if not isinstance(task_id, str) or not task_id:
        return None
    if verdict not in verdicts.VALID_VERDICTS:
        return None
    return VerdictKey(task_id=task_id, verdict=str(verdict), by=str(by or ""))


def poll_once(
    cursor: Cursor, *, backfill_minutes: float, now: datetime | None = None,
) -> PollResult:
    """One pass over every history and verdict file, from `cursor` forward.

    Pure and synchronous by design (rule 2): no notify, no broadcast, no
    network. The caller runs this via `asyncio.to_thread` and does the
    announcing itself, which is what keeps a slow phone send from blocking
    the read. `now` is the instant this poll began, which becomes the next
    poll's anchor; taken before the files are read, so a row written while
    they are being read is still after it.
    """
    began = now or datetime.now(timezone.utc)
    offsets = dict(cursor.offsets)
    floor = cursor.polled_at_dt() - timedelta(minutes=backfill_minutes)
    closes: list[ClosedTask] = []
    keys: list[VerdictKey] = []
    parse_errors = 0

    for kind, root, parse in (
        (_HISTORY_KIND, history_dir(), lambda row: _parse_close(row, floor)),
        (_VERDICTS_KIND, verdicts_dir(), _parse_key),
    ):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.jsonl")):
            key = _key_for(kind, path)
            start = offsets.get(key, 0)
            lines, new_offset = _read_new_lines(path, start)
            offsets[key] = new_offset
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    parse_errors += 1
                    continue
                if not isinstance(row, dict):
                    parse_errors += 1
                    continue
                parsed = parse(row)
                if parsed is None:
                    continue
                if kind == _HISTORY_KIND:
                    closes.append(parsed)  # type: ignore[arg-type]
                else:
                    keys.append(parsed)  # type: ignore[arg-type]

    new_cursor = Cursor(offsets=offsets, polled_at=began.isoformat())
    return PollResult(
        cursor=new_cursor, closes=tuple(closes), keys=tuple(keys), parse_errors=parse_errors,
    )


async def _announce_close(app: Any, close: ClosedTask) -> None:
    """`task_closed`: the phone message plus the WS envelope the Day room
    refetches on. Independent of `_announce_key` and of every other row in
    the same poll: one failing here never stops the rest (rule 5)."""
    from tesseract.orchestrator.autonomy.broadcast import broadcast_close_event

    notifier = app.get("outbound_notifier") if hasattr(app, "get") else None
    if notifier is not None:
        try:
            await notifier.notify("task_closed", {
                "item_id": close.item_id,
                "goal": close.goal[:200],
                "outcome": close.outcome,
                "verified_by": close.verified_by,
            })
        except Exception:  # noqa: BLE001
            log.exception("outcome_watch: could not notify the operator for %s", close.item_id)
    try:
        await broadcast_close_event(app, close.item_id, close.outcome, close.verified_by)
    except Exception:  # noqa: BLE001
        log.exception("outcome_watch: could not broadcast the close for %s", close.item_id)


async def _announce_key(app: Any, key: VerdictKey) -> None:
    from tesseract.orchestrator.autonomy.broadcast import broadcast_verdict_event

    try:
        await broadcast_verdict_event(app, key.task_id, key.verdict, key.by)
    except Exception:  # noqa: BLE001
        log.exception("outcome_watch: could not broadcast the key for %s", key.task_id)


class OutcomeWatcher:
    """The background loop. `start()`/`stop()` mirror every other Mirror
    background task: a single `asyncio.Task`, cancelled cleanly on shutdown.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._task: asyncio.Task | None = None
        self._cursor: Cursor | None = None
        self._saved: Cursor | None = None
        self._saved_at = 0.0
        self._seconds = 0.0
        self._max_seconds = 0.0
        self._backfill = 0.0

    async def start(self) -> None:
        if self._task is not None:
            return
        # Read before the task exists (rule 6): a missing key raises here,
        # where the caller can record it, not later inside the task.
        self._seconds = watch_seconds()
        self._max_seconds = watch_max_seconds()
        self._backfill = watch_backfill_minutes()
        self._cursor = await asyncio.to_thread(load_or_init_cursor, datetime.now(timezone.utc))
        self._saved = self._cursor
        self._saved_at = asyncio.get_running_loop().time()
        self._task = asyncio.create_task(self._run(), name="outcome_watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(self) -> None:
        interval = configured = self._seconds
        max_interval = self._max_seconds
        backfill = self._backfill
        failures = 0
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            try:
                await self._poll(backfill)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                failures += 1
                if failures == MAX_CONSECUTIVE_FAILURES:
                    log.exception(
                        "outcome_watch: %d consecutive poll failures, backing off",
                        failures,
                    )
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    interval = min(interval * 2, max_interval)
                continue
            if failures:
                failures = 0
                interval = configured

    async def _poll(self, backfill_minutes: float) -> None:
        assert self._cursor is not None
        result = await asyncio.to_thread(
            poll_once, self._cursor, backfill_minutes=backfill_minutes,
        )
        for close in result.closes:
            try:
                await _announce_close(self._app, close)
            except Exception:  # noqa: BLE001
                log.exception("outcome_watch: announcing a close raised for %s", close.item_id)
        for key in result.keys:
            try:
                await _announce_key(self._app, key)
            except Exception:  # noqa: BLE001
                log.exception("outcome_watch: announcing a key raised for %s", key.task_id)
        self._cursor = result.cursor
        # Rule 4: save when something arrived or an offset moved, otherwise at
        # most once per `watch_max_seconds`.
        now = asyncio.get_running_loop().time()
        moved = self._saved is None or dict(result.cursor.offsets) != dict(self._saved.offsets)
        stale = now - self._saved_at >= (self._max_seconds or 0.0)
        if moved or result.closes or result.keys or stale:
            await asyncio.to_thread(save_cursor, result.cursor)
            self._saved = result.cursor
            self._saved_at = now


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "ClosedTask",
    "Cursor",
    "OutcomeWatcher",
    "PollResult",
    "VerdictKey",
    "cursor_path",
    "initial_cursor",
    "load_cursor",
    "load_or_init_cursor",
    "poll_once",
    "save_cursor",
    "watch_backfill_minutes",
    "watch_max_seconds",
    "watch_seconds",
]
