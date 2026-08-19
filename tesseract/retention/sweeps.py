"""One function per tree the retention table ages.

Each is handed the window and the action the table resolved and reports what it
did. None of them decides either, and none of them reads config — that is the
whole reason the table can be read as an answer to "what does this machine
throw away".

The mechanics differ per tree because the trees differ: dated filenames, file
mtimes, a directory's newest child, and one growing file whose rows carry their
own timestamps. Four shapes, which is exactly why the *policy* had to be lifted
out of them.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tesseract.retention.policy import Action, Swept

log = logging.getLogger(__name__)

_DAY_S = 86400.0


def _retire(path: Path, action: Action, archive_dir: Path) -> Swept:
    """Delete or move one file, and never raise for one bad file."""
    try:
        if action is Action.DELETE:
            path.unlink()
            return Swept(removed=1)
        archive_dir.mkdir(parents=True, exist_ok=True)
        path.replace(archive_dir / path.name)
        return Swept(moved=1)
    except OSError as exc:
        log.warning("retention: %s failed for %s: %s", action.value, path, exc)
        return Swept(failed=1)


def observer_logs(keep_days: int, action: Action) -> Swept:
    """`<logs>/observer/YYYY-MM-DD.jsonl` — one file per day, dated by name.

    The name is the date, so this reads the stem rather than an mtime: a file
    touched by a backup or a sync would otherwise look young forever. A stem
    that is not a date is left alone.
    """
    from tesseract.brain.observer import _observer_log_dir

    root = _observer_log_dir()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for path in sorted(root.glob("*.jsonl")):
        try:
            stamped = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if stamped < cutoff:
            total += _retire(path, action, root / "archive")
    return total


def sessions(keep_days: int, action: Action) -> Swept:
    """The live chat drawer. Always an archive — `may_delete` is False.

    Archiving is a FLAG on the record, not a move. The old version relocated
    files into `sessions/archive/YYYY-MM/`, which made a conversation's
    location encode its state — so its path changed when nothing about the
    conversation had. `chat_store.archive_stale_open_chats` flips
    `archived: true` in place, the drawer's archive view already renders it,
    and the chat keeps the id and the creation stamp it was born with.

    `keep_days` still means days since activity, which is what it meant when
    it moved files, so an operator's `retention.yaml` window is unchanged.
    Counted as `moved` because that is this table's word for "aged out but
    still here" — nothing is deleted, and the count is chats, not files.
    """
    from tesseract.mirror.server import chat_store

    if not chat_store.chats_dir().is_dir():
        return Swept()
    return Swept(moved=chat_store.archive_stale_open_chats(keep_days=keep_days))


def lane_archives(keep_days: int, action: Action) -> Swept:
    """`home/controller/lanes-archive/<YYYY-MM>/<lane>/` — whole directories.

    Aged on the newest file anywhere inside, not on the directory's own mtime:
    on Windows a directory's mtime does not follow a write into a subdirectory,
    so the directory looks stale while the transcript inside is still being
    appended to.
    """
    from tesseract.janitor.scratch import _rmtree
    from tesseract.paths import home_dir

    root = home_dir() / "controller" / "lanes-archive"
    if not root.is_dir():
        return Swept()
    # OUTSIDE the swept root, not a `kept/` subdirectory of it. A destination
    # inside `root` matches `root.glob("*/*")` on the next run, so a kept month
    # would be re-swept as though it were a lane — nested one level deeper each
    # cycle under ARCHIVE, and under DELETE removed outright, taking everything
    # a previous run had deliberately kept. Structure rather than a guard: no
    # later change to this glob can re-introduce it.
    kept_root = root.parent / "lanes-kept"
    cutoff = time.time() - keep_days * _DAY_S
    total = Swept()
    for lane in sorted(root.glob("*/*")):
        if not lane.is_dir():
            continue
        try:
            if _newest_mtime(lane) >= cutoff:
                continue
            if action is Action.DELETE:
                _rmtree(lane)
                total += Swept(removed=1)
            else:
                destination = kept_root / lane.parent.name
                destination.mkdir(parents=True, exist_ok=True)
                lane.replace(destination / lane.name)
                total += Swept(moved=1)
        except OSError as exc:
            log.warning("retention: lane archive %s failed: %s", lane, exc)
            total += Swept(failed=1)
    _drop_empty_months(root)
    return total


def backend_logs(keep_days: int, action: Action) -> Swept:
    """`<logs>/backend/*.log*`, under BOTH log roots.

    `*.log*` rather than `*.log`: the rotating handler names its generations
    `<file>.log.1`..`.log.3`, so the narrower glob pruned each boot's live file
    and kept every rotated generation forever.
    """
    from tesseract.paths import home_logs_root, runtime_logs_root

    cutoff = time.time() - keep_days * _DAY_S
    total = Swept()
    for root in (home_logs_root(), runtime_logs_root()):
        for path in sorted(root.glob("backend/*.log*")):
            try:
                if not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            total += _retire(path, action, root / "backend" / "archive")
    return total


def approvals_ledger(keep_days: int, action: Action) -> Swept:
    """One growing file, rolled by month. Rows are moved, never dropped.

    The roll itself is `approval_log.roll_older_than`, on the module that owns
    the file and its lock. Doing it here would mean read-partition-rewrite
    without that lock, and a row appended by a live tool call between the read
    and the replace would land in neither the archive nor the ledger — a
    lost-write in exactly the file that must not lose one.

    `may_delete` is False on this tree, so `action` can only be ARCHIVE here.
    It is still read rather than assumed, because a sweep that ignores its
    argument keeps ignoring it after somebody edits the registry.
    """
    from tesseract.permissions.approval_log import roll_older_than

    if action is not Action.ARCHIVE:
        raise ValueError(
            "the approval ledger is archived, never deleted — "
            "`may_delete=False` should have refused this at load"
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    return Swept(moved=roll_older_than(cutoff))


def scheduler_runs(keep_days: int, action: Action) -> Swept:
    """`<logs>/schedule/runs.jsonl` — one growing file, one row per job run.

    The largest log this machine keeps, and the one every night reads whole.
    The roll itself is `scheduler/log.prune_older_than`, on the module that owns
    the file and its lock, for the same reason the approval ledger's is: a run recorded
    between the read and the replace would be lost, and at the anchor hour the
    nightly row is not the only one firing.

    **A day is only pruned once its rollup exists.** `daily_writer` writes a
    `Daily rollup <date>` entry into the daily log layer every night; a day
    with no rollup keeps its rows, and the count of those is reported rather
    than swallowed — a summary that silently did not happen is how a log
    becomes the only record and then stops being one.
    """
    from tesseract.scheduler.log import prune_older_than

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    retired, held = prune_older_than(
        cutoff,
        summarised=_day_has_rollup,
        archive=action is Action.ARCHIVE,
    )
    if held:
        log.info(
            "retention: kept %d scheduler run(s) past the window — no daily "
            "rollup was written for their day",
            held,
        )
    return Swept(moved=retired) if action is Action.ARCHIVE else Swept(removed=retired)


def _day_has_rollup(day: date) -> bool:
    """Whether `daily_writer` has summarised `day` into the daily log layer."""
    from tesseract.paths import log_dir

    path = log_dir("sessions") / f"{day.isoformat()}.jsonl"
    if not path.is_file():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                if json.loads(line).get("type") == "scheduler":
                    return True
            except ValueError:
                continue
    except OSError:
        return False
    return False


def watchman_reports(keep_days: int, action: Action) -> Swept:
    """`home/autonomy/watchman/<stamp>.md` and its `evidence/<stamp>-*.md`.

    One summary per hourly pass plus a file per finding, so the set grows with
    uptime and nothing else has ever removed one. Aged on the NAME, which is
    the sweep timestamp — an mtime would make a file a backup touched look
    young forever.

    `latest.json` is never swept: it is the live pointer the brief reads, not a
    dated artifact, and it always describes the most recent pass.
    """
    from tesseract.orchestrator.watchman.report import watchman_dir

    root = watchman_dir()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for directory, pattern in ((root, "*.md"), (root / "evidence", "*.md")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob(pattern)):
            stamped = _stamp_date(path.stem)
            if stamped is not None and stamped < cutoff:
                total += _retire(path, action, root / "archive" / directory.name)
    return total


def _stamp_date(stem: str) -> date | None:
    """The date out of a `YYYY-MM-DDTHHMM` stem, or `None` if it is not one.

    Evidence files carry a suffix (`...T0949-backend-boots-13279ff5`), so this
    reads the leading date rather than parsing the whole stem.
    """
    try:
        return date.fromisoformat(stem[:10])
    except ValueError:
        return None


def _newest_mtime(root: Path) -> float:
    newest = root.stat().st_mtime
    for path in root.rglob("*"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def usage_ledger(keep_days: int, action: Action) -> Swept:
    """`<logs>/usage/tools.jsonl` — one growing file, one row per tool call.

    The prune itself is `brain/tool_usage.prune_older_than`, on the module that
    owns the file and its lock, for the same reason the approval ledger's and
    the scheduler's are: a call recorded between the read and the replace would
    land in neither.

    Deleted rather than archived, and this is the one ledger where that is
    right. It holds `at_utc`, `tool`, `session_id` and nothing else — no
    inputs, no outputs — so an old row answers no question a new one cannot.
    The approval ledger is archived because an investigation reads it; nothing
    investigates this.
    """
    from tesseract.brain.tool_usage import prune_older_than

    if action is not Action.DELETE:
        raise ValueError(
            "the usage ledger is pruned in place — there is no archive for it, "
            "because a row carries no evidence worth keeping behind a window"
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    return Swept(removed=prune_older_than(cutoff))


def _drop_empty_months(root: Path) -> None:
    for month in root.glob("*"):
        try:
            if month.is_dir() and not any(month.iterdir()):
                month.rmdir()
        except OSError:
            pass


__all__ = [
    "approvals_ledger",
    "backend_logs",
    "lane_archives",
    "observer_logs",
    "scheduler_runs",
    "sessions",
    "usage_ledger",
    "watchman_reports",
]
