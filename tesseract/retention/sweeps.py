"""One function per tree the retention table ages.

Each is handed the window and the action the table resolved and reports what it
did. None of them decides either, and none of them reads config — that is the
whole reason the table can be read as an answer to "what does this machine
throw away".

The mechanics differ per tree because the trees differ: dated filenames, file
mtimes, a directory's newest child, and one growing file whose rows carry their
own timestamps. Four shapes, which is exactly why the *policy* had to be lifted
out of them.

**Where a tree lives is declared once, beside the sweep that ages it.** Every
`*_roots()` below is what its own sweep walks, and it is also what the panel
measures and what the panel subtracts when it asks which directories nothing
has decided about. A second answer to "where does this tree live" would drift
the first time a path moved, and the surface that exists to catch an
undeclared tree would be the thing declaring one.

Every root is resolved at call time. A module-level path freezes the tree
before a relocated home is known, which is the same defect the log writers
already avoid.
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


def observer_logs_roots() -> tuple[Path, ...]:
    from tesseract.brain.observer import _observer_log_dir

    return (_observer_log_dir(),)


def observer_logs(keep_days: int, action: Action) -> Swept:
    """`<logs>/observer/YYYY-MM-DD.jsonl` — one file per day, dated by name.

    The name is the date, so this reads the stem rather than an mtime: a file
    touched by a backup or a sync would otherwise look young forever. A stem
    that is not a date is left alone.
    """
    (root,) = observer_logs_roots()
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


def sessions_roots() -> tuple[Path, ...]:
    from tesseract.mirror.server import chat_store

    return (chat_store.chats_dir(),)


def sessions(keep_days: int, action: Action) -> Swept:
    """The live conversations rail. Always an archive — `may_delete` is False.

    Archiving is a FLAG on the record, not a move. The old version relocated
    files into `sessions/archive/YYYY-MM/`, which made a conversation's
    location encode its state — so its path changed when nothing about the
    conversation had. `chat_store.archive_stale_open_chats` flips
    `archived: true` in place, the rail's archive tab already renders it,
    and the chat keeps the id and the creation stamp it was born with.

    `keep_days` still means days since activity, which is what it meant when
    it moved files, so an operator's `retention.yaml` window is unchanged.
    Counted as `moved` because that is this table's word for "aged out but
    still here" — nothing is deleted, and the count is chats, not files.

    `may_delete` is False on this tree, so `action` can only be ARCHIVE. It is
    still read rather than assumed, for the reason `approvals_ledger` gives:
    a sweep that ignores its argument keeps ignoring it after somebody edits
    the registry.
    """
    from tesseract.mirror.server import chat_store

    if action is not Action.ARCHIVE:
        raise ValueError(
            "a conversation is archived, never deleted — "
            "`may_delete=False` should have refused this at load"
        )

    if not chat_store.chats_dir().is_dir():
        return Swept()
    return Swept(moved=chat_store.archive_stale_open_chats(keep_days=keep_days))


def lane_archives_roots() -> tuple[Path, ...]:
    from tesseract.paths import home_dir

    controller = home_dir() / "controller"
    return (controller / "lanes-archive", controller / "lanes-kept")


def lane_archives(keep_days: int, action: Action) -> Swept:
    """`home/controller/lanes-archive/<YYYY-MM>/<lane>/` — whole directories.

    Aged on the newest file anywhere inside, not on the directory's own mtime:
    on Windows a directory's mtime does not follow a write into a subdirectory,
    so the directory looks stale while the transcript inside is still being
    appended to.
    """
    from tesseract.janitor.scratch import _rmtree

    # The destination is OUTSIDE the swept root, not a `kept/` subdirectory of
    # it. A destination inside `root` matches `root.glob("*/*")` on the next
    # run, so a kept month would be re-swept as though it were a lane — nested
    # one level deeper each cycle under ARCHIVE, and under DELETE removed
    # outright, taking everything a previous run had deliberately kept.
    # Structure rather than a guard: no later change to this glob can
    # re-introduce it.
    root, kept_root = lane_archives_roots()
    if not root.is_dir():
        return Swept()
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


def worker_archives_roots() -> tuple[Path, ...]:
    from tesseract.orchestrator.workers.paths import worktrees_archive_dir

    return (worktrees_archive_dir(),)


def worker_archives(keep_days: int, action: Action) -> Swept:
    """`home/worktrees-archive/<worker>/` — a whole copy of the code tree per
    delegated task that edited code.

    The one tree here whose window is not the whole rule, which is why it is
    the one tree with a sweep it does not own. A task that FAILED keeps its
    copy however old it is, because that copy is the only place its work
    survives; what ages is the ones that finished. `prune_archives` has always
    made that distinction and nothing has ever called it, so the archive grew
    without limit while the rule sat there working.

    So the age decision stays there and this reports what it did. `held` is
    what the window reached and the sweep kept anyway, which here means a task
    that did not finish cleanly, or one whose record has gone: with no record
    the prune cannot tell the two apart and keeps the copy, because losing a
    worker's only output to a missing file is the worse of the two mistakes.

    `action` is unused. The archive is already where things are moved TO, so
    there is nowhere to archive one to, and the table declares delete as the
    only action it takes.
    """
    from tesseract.orchestrator.workers.record import load_record
    from tesseract.orchestrator.workers.worktree import (
        iter_archive_entries,
        prune_archives,
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    records = {}
    reached = 0
    failed = 0
    for worker_id, _entry, mtime in iter_archive_entries():
        if mtime <= cutoff:
            reached += 1
        try:
            record = load_record(worker_id)
        except (OSError, ValueError) as exc:
            # A record that is there and unreadable is not a record that is
            # gone. Both leave the copy in place; only this one is a fault.
            log.warning("retention: worker record %s could not be read: %s", worker_id, exc)
            failed += 1
            continue
        if record is not None:
            records[worker_id] = record

    pruned = prune_archives(retention_days=keep_days, records=records)
    return Swept(
        removed=len(pruned),
        held=max(reached - len(pruned) - failed, 0),
        failed=failed,
    )


def backend_logs_roots() -> tuple[Path, ...]:
    from tesseract.paths import home_logs_root, runtime_logs_root

    return (home_logs_root() / "backend", runtime_logs_root() / "backend")


def backend_logs(keep_days: int, action: Action) -> Swept:
    """`<logs>/backend/*.log*`, under BOTH log roots.

    `*.log*` rather than `*.log`: the rotating handler names its generations
    `<file>.log.1`..`.log.3`, so the narrower glob pruned each boot's live file
    and kept every rotated generation forever.
    """
    cutoff = time.time() - keep_days * _DAY_S
    total = Swept()
    for root in backend_logs_roots():
        for path in sorted(root.glob("*.log*")):
            try:
                if not path.is_file() or path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            total += _retire(path, action, root / "archive")
    return total


def approvals_ledger_roots() -> tuple[Path, ...]:
    from tesseract.permissions.approval_log import ledger_path

    path = ledger_path()
    return (path, path.parent / "approvals-archive")


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


def scheduler_runs_roots() -> tuple[Path, ...]:
    from tesseract.scheduler.log import runs_path

    path = runs_path()
    return (path, path.parent / "runs-archive")


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
    swept = (
        Swept(moved=retired) if action is Action.ARCHIVE else Swept(removed=retired)
    )
    # A log line is not a surface. This count is the one number that says the
    # summarise-then-prune contract is holding, so it travels with the sweep's
    # own result rather than only into the backend log, where nobody looking at
    # what the machine throws away would find it.
    return swept + Swept(held=held)


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


def watchman_reports_roots() -> tuple[Path, ...]:
    from tesseract.orchestrator.watchman.report import watchman_dir

    return (watchman_dir(),)


def watchman_reports(keep_days: int, action: Action) -> Swept:
    """`home/autonomy/watchman/<stamp>.md` and its `evidence/<stamp>-*.md`.

    One summary per hourly pass plus a file per finding, so the set grows with
    uptime and nothing else has ever removed one. Aged on the NAME, which is
    the sweep timestamp — an mtime would make a file a backup touched look
    young forever.

    `latest.json` is never swept: it is the live pointer the brief reads, not a
    dated artifact, and it always describes the most recent pass.
    """
    (root,) = watchman_reports_roots()
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


def conscience_roots() -> tuple[Path, ...]:
    from tesseract.paths import log_dir

    return (log_dir("conscience"),)


def conscience(keep_days: int, action: Action) -> Swept:
    """`<logs>/conscience/drift-*.jsonl` — the drift check's own record.

    One row per run of a check that runs on a clock, in files named by date,
    and until now nothing removed one. The watchman reads only the LATEST row
    (drift is a state, not a count), so what a window removes here is history
    behind a verdict that stays.

    Aged on the name rather than the mtime, like every other dated artifact:
    an mtime makes a file that a backup touched look young forever.
    """
    (root,) = conscience_roots()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for path in sorted(root.glob("drift-*.jsonl")):
        stamped = _stamp_date(path.stem.replace("drift-", "", 1))
        if stamped is not None and stamped < cutoff:
            total += _retire(path, action, root / "archive")
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


def usage_ledger_roots() -> tuple[Path, ...]:
    from tesseract.brain.tool_usage import usage_path

    return (usage_path(),)


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


def agenda_records_roots() -> tuple[Path, ...]:
    from tesseract.orchestrator.autonomy.paths import agenda_archive_dir, agenda_index_path

    return (agenda_archive_dir(), agenda_index_path())


def agenda_records(keep_days: int, action: Action) -> Swept:
    """`agenda/archive/YYYY-MM/<id>.json`, and the rows of `agenda/index.jsonl`
    that describe them.

    A bucket goes once every day of its month is past the window. Before a
    record is deleted, its row is written to `agenda/history/` if it has none,
    so an item closed before the history existed still leaves one behind; a
    record that cannot be read well enough to write a row is KEPT, because
    deleting it would leave nothing at all. The index rows about a record go
    when the record goes, by id and not by date, so the two age together and
    the completion figures that read the index never lose an item whose record
    is still on disk. Pruned in place under the store's own lock.

    Deleted rather than archived: the full record is what is being aged, and
    an archive of an archive would be the same bytes one directory over.
    `active/` is never touched; a file there is work still owed.
    """
    from tesseract.orchestrator.autonomy.agenda_history import record_closed
    from tesseract.orchestrator.autonomy.agenda_store import prune_index_rows
    from tesseract.orchestrator.autonomy.models import AgendaItem

    if action is not Action.DELETE:
        raise ValueError(
            "agenda records are deleted, not archived: the row that outlives "
            "them is written to agenda/history/ first, and an archive of the "
            "archive would be the same bytes one directory over"
        )
    archive_root, _index = agenda_records_roots()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    gone: set[str] = set()
    if archive_root.is_dir():
        for month_dir in sorted(archive_root.iterdir()):
            if not month_dir.is_dir():
                continue
            try:
                year, month = (int(part) for part in month_dir.name.split("-"))
                first_of_next = date(year + (month // 12), (month % 12) + 1, 1)
            except (ValueError, TypeError):
                continue
            if first_of_next > cutoff:
                continue
            for path in sorted(month_dir.glob("*.json")):
                try:
                    item = AgendaItem.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                    record_closed(item)
                except Exception as exc:  # noqa: BLE001 - keep what cannot be summarised
                    log.warning("retention: kept %s, no row could be written for it: %s", path, exc)
                    total += Swept(held=1)
                    continue
                retired = _retire(path, action, archive_root)
                if retired.removed:
                    gone.add(item.id)
                total += retired
            try:
                if not any(month_dir.iterdir()):
                    month_dir.rmdir()
            except OSError:
                pass
    total += Swept(removed=prune_index_rows(gone))
    return total


def turn_manifests_roots() -> tuple[Path, ...]:
    from tesseract.orchestrator.turns import turns_root

    return (turns_root(),)


def turn_manifests(keep_days: int, action: Action) -> Swept:
    """`runtime/turns/YYYY-MM-DD/<turn id>.json` — one file per finished turn,
    in a directory named for the day it started.

    Dated by directory name rather than by mtime, for the reason the observer
    logs are: a backup or a sync touching a file would make an old record look
    young forever.

    `open/` is never swept. A file there is a turn that has not been closed
    yet, which is either running right now or waiting for the next boot to
    recover it, and a window that removed one would throw away the only thing
    that knows a person is still waiting.
    """
    (root,) = turn_manifests_roots()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            stamped = date.fromisoformat(day_dir.name)
        except ValueError:
            continue  # `open/`, and anything else that is not a day
        if stamped >= cutoff:
            continue
        for path in sorted(day_dir.glob("*.json")):
            total += _retire(path, action, root / "archive" / day_dir.name)
        try:
            if not any(day_dir.iterdir()):
                day_dir.rmdir()
        except OSError:
            pass
    return total


def loop_stalls_roots() -> tuple[Path, ...]:
    from tesseract.orchestrator import loop_stalls as record

    return (record.root(),)


def loop_stalls(keep_days: int, action: Action) -> Swept:
    """`<logs>/loop-stalls/stalls-YYYY-MM-DD.jsonl` — one row per block of the
    event loop long enough to matter.

    Aged on the name, like every other dated artifact here: an mtime makes a
    file a backup touched look young forever.
    """
    (root,) = loop_stalls_roots()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for path in sorted(root.glob("stalls-*.jsonl")):
        stamped = _stamp_date(path.stem.replace("stalls-", "", 1))
        if stamped is not None and stamped < cutoff:
            total += _retire(path, action, root / "archive")
    return total


def _drop_empty_months(root: Path) -> None:
    for month in root.glob("*"):
        try:
            if month.is_dir() and not any(month.iterdir()):
                month.rmdir()
        except OSError:
            pass


__all__ = [
    "agenda_records",
    "agenda_records_roots",
    "approvals_ledger",
    "approvals_ledger_roots",
    "backend_logs",
    "backend_logs_roots",
    "conscience",
    "conscience_roots",
    "lane_archives",
    "lane_archives_roots",
    "loop_stalls",
    "loop_stalls_roots",
    "observer_logs",
    "observer_logs_roots",
    "scheduler_runs",
    "scheduler_runs_roots",
    "sessions",
    "sessions_roots",
    "turn_manifests",
    "turn_manifests_roots",
    "usage_ledger",
    "usage_ledger_roots",
    "watchman_reports",
    "watchman_reports_roots",
]
