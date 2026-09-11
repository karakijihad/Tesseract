"""What this machine throws away, and what nothing has decided about.

``GET /api/autonomy/retention`` is the room. Two different things get called
pruning and only one of them was visible: the agenda's admission gate has had
its own feed and its own room since before this phase, and what a *file* sweep
did had no route, no surface and no log an operator would find. The machine
deletes things nightly and the only way to know what was to notice something
missing.

Three bands, and the third is why the room earns its place:

* **What ages** — every tree in the retention table, with the window the
  operator set, what the last sweep did to it, and what it measures now. The
  prose is `retention/policy.py`'s, which is the same prose the config file
  points at, so nothing here describes a tree in its own words.
* **Kept on purpose** — the trees declared as a decision never to age. Small
  per event, or bounded some other way, with the reason.
* **Nothing has decided** — everything else under the log trees. This is the
  band that had to exist: five trees had a window and the two that grew
  fastest did not, which is exactly the shape a surface catches and a
  directory listing cannot, because from a listing a decision and an oversight
  look identical.

**The sizes are a walk and the walk is not free.** Roughly a second over the
whole tree on the machine this was built on, so it happens off the loop and is
held for longer than the room polls. A file tree does not move between polls.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from aiohttp import web

from tesseract import paths
from tesseract.mirror.server.routes._bands import band_for
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes._isotime import parse as _parse
from tesseract.orchestrator.liveness import OperationalState, label_of
from tesseract.orchestrator.obligation import as_payload as wants
from tesseract.retention import record
from tesseract.retention.policy import KEPT, RetentionError, load_live

log = logging.getLogger(__name__)

_band = band_for("retention")

# How long one reading of the tree stands. The room polls slower than this, so
# in practice every poll pays for a walk it did not share; the ceiling is here
# so two surfaces open at once, or a reload, do not each pay for one.
SIZES_HELD_FOR = 120.0

def scanned_roots() -> tuple[tuple[str, Path], ...]:
    """Where a tree with no window would be found, and what to call it.

    Not every swept tree lives here — conversations and turn records do not —
    but everything that has grown unnoticed so far has, and a scan of the whole
    home tree would report the vault and the memory store as undecided, which
    they are not: they are the library, and nothing ages the library.

    Both halves of the log tree are named apart, because they are two
    directories both called `logs` and a row saying `logs/backend` would be
    two different trees on one line.
    """
    return (
        ("logs", paths.home_logs_root()),
        ("runtime logs", paths.runtime_logs_root()),
        ("autonomy", paths.home_dir() / "autonomy"),
    )


def _row(
    name: str,
    state: OperationalState,
    said: str,
    *,
    detail: str = "",
    at: datetime | None = None,
    value: str = "",
    tree: str = "",
    days: int | None = None,
) -> dict[str, Any]:
    log_path = ""
    if state in (OperationalState.FAILED, OperationalState.DEGRADED):
        backend_log = paths.runtime_logs_root() / "mirror-backend.log"
        if backend_log.is_file():
            log_path = backend_log.resolve().as_posix()
        else:
            said += " The backend log is unavailable, so its errors cannot be investigated here."
    return {
        "name": name,
        "state": state.value,
        "label": label_of(state),
        # What this row asks of whoever reads it, decided once in
        # `orchestrator/obligation.py` and the only input to its colour.
        **wants(state),
        "said": said,
        # The second line under the sentence: what would be lost if this
        # stopped being kept. It is the tree's own `why`, never composed here.
        "detail": detail,
        "at": _iso(at) if at else None,
        "value": value,
        # Set on the rows a window may be changed on, and on no others. The
        # key is what the control names the tree by, and the number is what
        # the field starts at. A row without them carries no control, which is
        # how "kept on purpose" and "nothing has decided" stay read-only
        # without the surface deciding that for itself.
        "tree": tree,
        "days": days,
        "logPath": log_path,
    }


def measure(owner: Any) -> str:
    """One tree's size, or nothing when it cannot say where it lives.

    Every `where` does a lazy import, because a root reaches into the session
    store or the observer and a cycle would make the config loader
    unimportable. So a moved module makes the callable raise, and one tree
    raising must cost that tree's number and not the whole room: this is the
    same guard `_declared` applies for the same reason, applied at the two
    other places a root is resolved.
    """
    try:
        return in_words(size_of(owner.where()))
    except Exception:  # noqa: BLE001 — one tree's number, never the room
        log.warning(
            "retention route: %r could not say where it lives", owner.key,
            exc_info=True,
        )
        return ""


def size_of(paths_: Iterable[Path]) -> int:
    """Bytes under these paths. A path that is gone is nothing, not an error:
    a tree the sweep has emptied is a real answer."""
    total = 0
    for path in paths_:
        try:
            if path.is_file():
                total += path.stat().st_size
                continue
            if not path.is_dir():
                continue
            for found in path.rglob("*"):
                try:
                    if found.is_file():
                        total += found.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return total


def in_words(size: int) -> str:
    """A size a person reads. Rounded, because the point is whether something
    is growing and not what it weighs to the byte."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _what_it_did(counts: dict[str, Any]) -> str:
    """What the last sweep did to one tree, from the counts it wrote down."""
    moved = int(counts.get("moved") or 0)
    removed = int(counts.get("removed") or 0)
    failed = int(counts.get("failed") or 0)
    held = int(counts.get("held") or 0)
    said: list[str] = []
    if removed:
        said.append(f"deleted {_plural(removed, 'thing', 'things')}")
    if moved:
        said.append(f"moved {_plural(moved, 'thing', 'things')} aside")
    if not said:
        said.append("found nothing old enough to touch")
    if held:
        said.append(
            f"and kept {_plural(held, 'row', 'rows')} past the window because "
            "no summary of their day had been written yet"
        )
    if failed:
        said.append(f"and could not touch {_plural(failed, 'thing', 'things')}")
    return "The last sweep " + ", ".join(said) + "."


def ages(last: record.Record) -> list[dict[str, Any]]:
    """Every tree with a window, and what the last sweep did to it."""
    try:
        policies = load_live()
    except RetentionError as exc:
        # A table that does not hold refuses to load, so the sweep is not
        # running at all. That is the loudest thing this room can say.
        return [
            _row(
                "the retention table",
                OperationalState.FAILED,
                f"nothing is being aged, because the table does not hold: {exc}",
            )
        ]

    counts_of: dict[str, dict[str, Any]] = {}
    errored: dict[str, str] = {}
    swept_at: datetime | None = None
    if isinstance(last, dict):
        raw = last.get("trees")
        if isinstance(raw, dict):
            counts_of = {k: v for k, v in raw.items() if isinstance(v, dict)}
        swept_at = _parse(last.get("observed_at"))
        for line in last.get("errors") or []:
            key, _, why = str(line).partition(":")
            errored[key.strip()] = why.strip()

    rows: list[dict[str, Any]] = []
    for policy in policies:
        tree = policy.tree
        counts = counts_of.get(tree.key)
        why_it_failed = errored.get(tree.key)
        if why_it_failed:
            state = OperationalState.FAILED
            did = (
                f"The last sweep could not touch it ({why_it_failed}), so "
                "nothing here was aged."
            )
        elif counts is None:
            # No record of this tree. Either nothing has swept here, or a
            # sweep ran before this tree was in the table.
            state = OperationalState.NOT_INSTRUMENTED
            did = "No sweep on this machine has reached it yet."
        elif int(counts.get("failed") or 0):
            state = OperationalState.DEGRADED
            did = _what_it_did(counts)
        else:
            state = OperationalState.IDLE
            did = _what_it_did(counts)
        rows.append(
            _row(
                tree.title,
                state,
                f"{policy.in_words()} {did}",
                detail=tree.why,
                at=swept_at if counts is not None else None,
                value=measure(tree),
                tree=tree.key,
                days=policy.keep_days,
            )
        )
    return rows


def kept() -> list[dict[str, Any]]:
    """The trees declared as a decision never to age."""
    return [
        _row(
            entry.title,
            OperationalState.IDLE,
            entry.why,
            value=measure(entry),
        )
        for entry in sorted(KEPT.values(), key=lambda e: e.key)
    ]


def _declared() -> list[Path]:
    """Every path either list claims, for subtracting from what is on disk.

    A tree whose roots cannot be resolved is skipped rather than raising: an
    import that fails would otherwise turn every path under it into a tree
    nobody has decided about, which is a room inventing work.
    """
    from tesseract.retention.policy import TREES

    out: list[Path] = []
    for owner in (*TREES.values(), *KEPT.values()):
        try:
            out.extend(owner.where())
        except Exception:  # noqa: BLE001 — a bad root costs its own row, not the room
            log.warning(
                "retention route: %r could not say where it lives", owner.key,
                exc_info=True,
            )
    return out


def _undeclared(directory: Path, declared: list[Path]) -> list[Path]:
    """What nothing in `declared` speaks for, under `directory`.

    Three cases per child, and the middle one is the reason this recurses
    rather than testing containment in both directions. A child a declaration
    sits INSIDE is only partly spoken for: `logs/schedule` holds the declared
    `runs.jsonl` and would hide anything else dropped beside it. So the scan
    goes in exactly where a declaration is, and no deeper.
    """
    try:
        children = sorted(directory.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for child in children:
        if any(child == root or child.is_relative_to(root) for root in declared):
            continue
        if child.is_dir() and any(root.is_relative_to(child) for root in declared):
            out.extend(_undeclared(child, declared))
            continue
        out.append(child)
    return out


def undecided() -> list[dict[str, Any]]:
    """What is under the log trees that neither list names.

    Biggest first, because the question this band answers is which of them is
    growing.
    """
    declared = _declared()
    found: list[tuple[int, str]] = []
    for label, root in scanned_roots():
        if not root.is_dir():
            continue
        for path in _undeclared(root, declared):
            found.append((size_of([path]), f"{label}/{path.relative_to(root).as_posix()}"))
    found.sort(key=lambda pair: (-pair[0], pair[1]))
    return [
        _row(
            name,
            # Nothing produces an answer about this one, which is the same
            # claim the panel makes about a department with no camera.
            OperationalState.NOT_INSTRUMENTED,
            "No retention rule covers this tree. No tool or control can give "
            "it a window or mark it as kept for good today. A code change must "
            "register its sweep or declare it kept; adding a window to "
            "config/retention.yaml alone is rejected.",
            value=in_words(size),
        )
        for size, name in found
    ]


#: One reading of the whole tree, when it was taken, and the state of every
#: file it was read from. Walking every log directory costs about a second,
#: and a file tree does not move between two polls of a room.
_cache: tuple[float, tuple[int, int], dict[str, Any]] | None = None
_lock = threading.Lock()


def _written_stamp() -> tuple[int, int]:
    """When each thing this reading is joined FROM was last written.

    A held reading is not only sizes. It carries the windows off
    `retention.yaml`, so a window changed from this room, from a channel or by
    hand would go on reading as the old one for two minutes — a control that
    looks broken. And it carries the last sweep's own counts off the record,
    so a sweep that finished inside the window would go on reading as the one
    before it. Both are producers this payload joins, so both expire it: a
    reading may not outlive anything it was read from.

    Nanoseconds, because a whole second is long enough for a window to be
    changed and the room polled inside one, and two writes a stamp cannot tell
    apart are a reading that outlives the table it was read from.
    """
    return (
        _mtime(paths.config_dir() / "retention.yaml"),
        _mtime(record.record_path()),
    )


def _mtime(path: Path) -> int:
    """Nanoseconds, or 0 for a file that is not there or cannot be read. Both
    of those are stable answers, so a missing file holds a reading rather than
    expiring it every poll."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _still_good(
    cached: tuple[float, tuple[int, int], dict[str, Any]] | None,
    at: float,
    stamp: tuple[int, int],
) -> bool:
    return cached is not None and at - cached[0] < SIZES_HELD_FOR and cached[1] == stamp


def bands(now: float | None = None) -> dict[str, Any]:
    """The three bands, read once and held."""
    global _cache

    at = now if now is not None else time.monotonic()
    stamp = _written_stamp()
    cached = _cache
    if _still_good(cached, at, stamp):
        return cached[2]  # type: ignore[index]
    with _lock:
        cached = _cache
        if _still_good(cached, at, stamp):
            return cached[2]  # type: ignore[index]
        last = record.read()
        payload = {
            "ages": ages(last),
            "kept": kept(),
            "undecided": undecided(),
            "lastSweepSaid": _last_sweep_said(last),
        }
        _cache = (at, stamp, payload)
        return payload


def _last_sweep_said(last: record.Record) -> str:
    """Whether anything has swept here at all, in one sentence.

    Three answers, not two. A record that is absent and one that could not be
    read are different claims about the same room, and the second is the one
    worth getting right: something IS sweeping and this cannot see what it did.
    """
    if last is None:
        return (
            "No sweep has run on this machine yet, so nothing below has been "
            "aged and the windows are what would happen tonight."
        )
    if isinstance(last, str):
        return last
    at = _parse(last.get("observed_at"))
    if at is None:
        return "A sweep has run and its record does not say when."
    return ""


async def get_retention(request: web.Request) -> web.Response:
    """The room, in three bands. The walk is off the loop."""
    (read,) = await asyncio.gather(asyncio.to_thread(bands), return_exceptions=True)
    payload = _band(
        read,
        {
            "ages": [],
            "kept": [],
            "undecided": [],
            "lastSweepSaid": (
                "What this machine ages could not be read, so what it throws "
                "away is not known rather than nothing."
            ),
        },
    )
    return web.json_response(payload)


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/retention", get_retention)


__all__ = [
    "SIZES_HELD_FOR",
    "ages",
    "bands",
    "get_retention",
    "in_words",
    "kept",
    "register",
    "scanned_roots",
    "size_of",
    "undecided",
]
