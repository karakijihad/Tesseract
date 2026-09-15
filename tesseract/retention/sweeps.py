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
    """Delete or move one file, and never raise for one bad file.

    **A DELETE-only tree still has to pass an `archive_dir`, and it is dead.**
    Several sweeps here raise unless `action is Action.DELETE`, so the ARCHIVE
    branch below is unreachable from them and the directory they name is never
    created. It is named rather than omitted because the parameter is not
    optional, and naming where a future ARCHIVE would put things beats inventing
    a path at the moment somebody widens that tree's `actions`. Said once here
    rather than at each call site, because it is a property of this function.
    """
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


# The four ledgers below are one growing JSONL each, and each is pruned by row
# through `lib/jsonl_rolls.prune_older_than` rather than by a function on the
# module that owns the file. That is a deliberate departure from what
# `usage_ledger`, `approvals_ledger` and `scheduler_runs` do, and the reason
# they do it does not apply here: they moved the prune onto the owning module to
# get at that module's LOCK, and none of these four has one, because nothing
# ever pruned them. Two of them are written by the supervisor in another
# process, where no lock either side could take would join them. So the prune
# holds its ground a different way — it stats the file before and after and
# abandons itself rather than write a file missing a row — and a lock added to
# four modules would buy nothing a reader could rely on.
#
# Each still names its own file, its own timestamp field and its own reason for
# a window. A shared sweep taking a directory from config is the thing
# GOVERNANCE.md §3 refuses, and this is not that.


def cost_ledger_roots() -> tuple[Path, ...]:
    from tesseract.paths import home_logs_root

    root = home_logs_root()
    # The live file first: the sweep unpacks it as the head so the glob has one
    # home rather than being written out again there.
    return (
        root / "cost-tracking.jsonl",
        *sorted(root.glob("cost-tracking.jsonl.before-recompute-*")),
    )


def cost_ledger(keep_days: int, action: Action) -> Swept:
    """`<logs>/cost-tracking.jsonl` — one row per paid model call, and the
    copies the recompute script takes before it rewrites the file.

    The largest thing under the log tree that nothing had decided about: 2.8 MB
    of live rows here, and 3.7 MB more in two copies from a script somebody ran
    once in September and never came back for.

    Two shapes, one window. The live file is pruned by row on `ts`. A
    `cost-tracking.jsonl.before-recompute-<stamp>` copy is retired on the stamp
    in its NAME, read with `_compact_stamp_date` off the full name rather than
    the stem: `Path.stem` reads that trailing part as a suffix and hands back
    `cost-tracking.jsonl`, so a stem-based sweep would date none of them and
    keep every copy for ever.

    **The floor is twice the widest window the panel draws.** `CostLedger.
    WINDOW_DAYS` tops out at a month and `windows()` compares each span against
    the SAME span immediately before it, so the month figure reads sixty days
    back. A window under that would leave the panel comparing this month
    against a period whose rows had been pruned, which reports a rise that did
    not happen rather than failing.

    DELETE only. The live file is pruned in place, and a copy taken before a
    rewrite is already what an archive would be.
    """
    if action is not Action.DELETE:
        raise ValueError(
            "the cost ledger is pruned in place, and a before-recompute copy "
            "is already the archive of one — `actions` should have refused "
            "this at load"
        )

    live, *backups = cost_ledger_roots()
    total = _pruned_rows(live, keep_days, "ts") if live.is_file() else Swept()
    cutoff_day = date.today() - timedelta(days=keep_days)
    for path in backups:
        stamped = _compact_stamp_date(path.name)
        if stamped is not None and stamped < cutoff_day:
            total += _retire(path, action, path.parent / "archive")
    return total


def _pruned_rows(path: Path, keep_days: int, field: str) -> Swept:
    """Rows older than the window, out of one unlocked ledger, and never a raise.

    `_retire` makes the same promise one file at a time, and for the same
    reason: a whole nightly pass over two dozen trees does not report itself
    failed because one file could not be written this minute.

    That is not theoretical on Windows, and it is measured rather than assumed:
    `os.replace` over a file another process holds open for an append fails with
    `PermissionError` and leaves the original whole. Two of these ledgers are
    appended to by the supervisor, so the collision is a real event with no row
    lost. It is reported as a failure for this tree and pruned tomorrow.

    The shared part is only the not-raising and the cutoff. Each sweep below
    still names its own file, its own dated field and its own reason for a
    window, which is what `GOVERNANCE.md` §2 asks of it.
    """
    from tesseract.lib.jsonl_rolls import prune_older_than

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    try:
        return Swept(removed=prune_older_than(path, cutoff, field))
    except OSError as exc:
        log.warning("retention: pruning %s failed: %s", path, exc)
        return Swept(failed=1)


def agent_invocations_roots() -> tuple[Path, ...]:
    from tesseract.agents.invocations import invocations_path

    return (invocations_path(),)


def agent_invocations(keep_days: int, action: Action) -> Swept:
    """`<logs>/agents/invocations.jsonl` — one row per agent card that was used
    to build a call.

    Its own module says this table is what eventually trims it. What that
    module also says, and what bounds the window from below, is that a name
    ABSENT from the file has never been invoked: `last_invocations` returns a
    mapping so absence reads as a state, and the roster's keep, rewrite or
    delete judgement is made on it. So a row removed does not merely shorten a
    count, it turns "used a while ago" into "never used". `floor_days` reads
    `COUNT_WINDOW_DAYS` off that module rather than restating it, so the window
    can never be set below the span the roster counts over.

    DELETE only. The row holds a name, an instant and the caller, so there is
    nothing in an old one an investigation would want back, which is the
    `usage_ledger` argument exactly.
    """
    if action is not Action.DELETE:
        raise ValueError(
            "the invocations ledger is pruned in place — there is no archive "
            "for it, and `actions` should have refused this at load"
        )
    (path,) = agent_invocations_roots()
    return _pruned_rows(path, keep_days, "ts")


def skill_usage_roots() -> tuple[Path, ...]:
    from tesseract.brain.skill_usage import usage_log_path

    return (usage_log_path(),)


def skill_usage(keep_days: int, action: Action) -> Swept:
    """`<logs>/skills/usage.jsonl` — one row per skill body the assistant read,
    and one per correction attributed to a skill afterwards.

    Read by the skill refinement job over a window of its own, set in
    `config/schedule.yaml`. That window is days and this one is months, so the
    job sees a whole span of evidence rather than the tail of one. `floor_days`
    is well above it deliberately: a window short enough to empty what the job
    reads would leave it deciding a skill keeps failing on two rows.

    DELETE only, like the invocations ledger beside it and for the same reason.
    """
    if action is not Action.DELETE:
        raise ValueError(
            "the skill usage ledger is pruned in place — there is no archive "
            "for it, and `actions` should have refused this at load"
        )
    (path,) = skill_usage_roots()
    return _pruned_rows(path, keep_days, "ts")


def janitor_sweeps_roots() -> tuple[Path, ...]:
    from tesseract.paths import log_dir

    return (log_dir("janitor") / "sweeps.jsonl",)


def janitor_sweeps(keep_days: int, action: Action) -> Swept:
    """`<runtime logs>/janitor/sweeps.jsonl` — one row per clean-up the janitor
    ran, which is one per supervisor boot.

    The watchman reads it for sweeps that ERRORED, within a window of hours, and
    a sweep that cleaned nothing is not a finding at all. So nothing reads an old
    row: what it answers is whether the clean-up is failing now.

    Written by the supervisor process while this runs in the backend, which is
    why the prune abandons itself rather than lose a row. The dated field is
    `started_at_utc`, not `ts`: a row is written when the sweep finishes and
    carries both, and the start is when the work it describes happened.

    DELETE only. The row is a count and a summary line, and there is no archive
    for one.
    """
    if action is not Action.DELETE:
        raise ValueError(
            "the janitor's record is pruned in place — there is no archive for "
            "it, and `actions` should have refused this at load"
        )
    (path,) = janitor_sweeps_roots()
    return _pruned_rows(path, keep_days, "started_at_utc")


def tokenjuice_audit_roots() -> tuple[Path, ...]:
    from tesseract.kernel.tokenjuice.audit import audit_dir

    return (audit_dir() / "audit.jsonl",)


def tokenjuice_audit(keep_days: int, action: Action) -> Swept:
    """`<runtime logs>/tokenjuice/audit.jsonl` — one row each time a tool result
    was shortened before it reached the model.

    The highest write rate of the four: a row per call rather than per boot or
    per day. Nothing in the runtime reads it, which is the point of a window
    here rather than a longer one. It exists so a person can check that a rule
    which shortens a result is shortening the right ones, and that question is
    asked about this week's calls.

    DELETE only. Every row is a before and after token count, so an old one
    holds nothing a new one does not.
    """
    if action is not Action.DELETE:
        raise ValueError(
            "the shortening audit is pruned in place — there is no archive for "
            "it, and `actions` should have refused this at load"
        )
    (path,) = tokenjuice_audit_roots()
    return _pruned_rows(path, keep_days, "ts")


def session_journal_roots() -> tuple[Path, ...]:
    from tesseract.paths import log_dir

    return (log_dir("sessions"),)


def session_journal(keep_days: int, action: Action) -> Swept:
    """`<logs>/sessions/` — the day by day record of your own sessions, and one
    journal per session of the background work it started. Two shapes:

    - `YYYY-MM-DD.jsonl`, one file per day, holding a line each time a session
      closed, a conversation was compacted, a boundary was consolidated, and
      once a night the summary of every background job that ran. Measured here:
      509 session-close lines in one day against one summary line.
    - `<session_id>/spawns.jsonl`, one directory per session, holding what it
      started and what came back. Read only when a session is resumed, to find
      work that vanished when the app restarted.

    **The nightly summary line is never removed, whatever the window says.**
    That is not tidiness, it is the thing that keeps the largest log on the
    machine ageing at all: `scheduler_runs` prunes a day of job rows only once
    `_day_has_rollup` finds that summary, and HOLDS the day for ever when it
    does not. Deleting a day file here would therefore stop a 5 MB log ageing,
    silently and permanently, from the far side of the table. So a day past the
    window keeps its summary lines and loses the rest, and the file goes only
    when there were none. Counted as `held`, which is this table's word for
    what a window reached and a sweep kept anyway.

    A day past the window has no writer: `append_log_entry` always files into
    TODAY's file, so nothing appends to a file this rewrites and the
    stat-guarded prune the supervisor's record needs is not needed here.

    The journals age on the newest file inside rather than the directory's own
    mtime, for the reason `lane_archives` gives: on Windows a directory's mtime
    does not follow a write into it, so a session still being written to looks
    stale.

    DELETE only. A day file is rewritten in place, and there is no archive for
    part of a file to move into.

    What is counted is lines for the day files and directories for the
    journals, which is the unit each of those loses things in. The table
    already mixes the two across trees (`usage_ledger` counts rows,
    `lane_archives` counts directories); saying so here is what keeps a reader
    of one number from taking it for the other.
    """
    from tesseract.janitor.scratch import _rmtree

    if action is not Action.DELETE:
        raise ValueError(
            "a day's record is rewritten in place, keeping the nightly "
            "summary — there is nowhere for part of a file to be archived to, "
            "and `actions` should have refused this at load"
        )

    (root,) = session_journal_roots()
    if not root.is_dir():
        return Swept()
    cutoff_day = date.today() - timedelta(days=keep_days)
    cutoff_mtime = time.time() - keep_days * _DAY_S
    total = Swept()

    for child in sorted(root.iterdir()):
        if child.is_dir():
            if child.name == "archive":
                continue
            try:
                if _newest_mtime(child) >= cutoff_mtime:
                    continue
                _rmtree(child)
                total += Swept(removed=1)
            except OSError as exc:
                log.warning("retention: spawn journal %s failed: %s", child, exc)
                total += Swept(failed=1)
            continue
        if child.suffix != ".jsonl":
            continue
        stamped = _stamp_date(child.stem)
        if stamped is None or stamped >= cutoff_day:
            continue
        total += _prune_day_keeping_summaries(child)
    return total


def _prune_day_keeping_summaries(path: Path) -> Swept:
    """Drop everything in one day's record except the nightly job summaries.

    The summary rows are what `_day_has_rollup` reads, so they outlive the
    window. A row this cannot read as JSON is KEPT: an unreadable line is not
    evidence it is disposable, and the cost of guessing wrong is the rollup.
    """
    from tesseract.lib.jsonl_rolls import rewrite

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("retention: could not read %s: %s", path, exc)
        return Swept(failed=1)

    keep: list[str] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            summary = json.loads(line).get("type") == "scheduler"
        except ValueError:
            summary = True
        if summary:
            keep.append(line)
        else:
            dropped += 1
    if not dropped:
        return Swept()
    try:
        if keep:
            rewrite(path, keep)
            return Swept(removed=dropped, held=len(keep))
        path.unlink()
        return Swept(removed=dropped)
    except OSError as exc:
        log.warning("retention: pruning %s failed: %s", path, exc)
        return Swept(failed=1)


def supervisor_incidents_roots() -> tuple[Path, ...]:
    from tesseract.paths import log_dir

    return (log_dir("supervisor"),)


def supervisor_incidents(keep_days: int, action: Action) -> Swept:
    """`<runtime logs>/supervisor/` — everything the supervisor wrote down when
    the app was in trouble. Three shapes, one window, named here because a
    reader of this table should not have to guess which of them a row covers.

    - `backend-stack-<pid>-<stamp>.txt`, one file per stack it dumped over a
      backend that stopped answering. The bulk of the directory by an order of
      magnitude, and the reason it needs a window at all.
    - `crash-storm-archive/<stamp>.json`, one file per crash storm that was
      cleared. Rare by design, and kept for the same span as the stacks so a
      storm and the dumps around it go together rather than one outliving the
      other and describing half an outage.
    - `heartbeat-incidents.jsonl`, one row per health probe that failed enough
      times to matter. Pruned by row and never removed as a file, because it is
      one file for every incident there has ever been.

    The argument for a window is `loop_stalls`': written only when it happens,
    so a healthy machine adds nothing for weeks, and what it answers is whether
    this week is worse than last. A stack from the spring explains nothing
    about today.

    The two file shapes age on the stamp in the name and not on an mtime, like
    every dated artifact here. The row prune is `lib/jsonl_rolls.
    prune_older_than`, which abandons itself rather than lose a row: the
    supervisor appends to that file from its own process while this runs in the
    backend, so there is no lock the two could share.

    DELETE only, and `actions` says so at load. Not policy but capability, the
    `usage_ledger` distinction: the incident rows are pruned in place and there
    is no archive for a row to move into, so a tree that archived two of its
    three shapes and deleted from the third would be reporting one word for two
    different acts. The argument is still read rather than assumed, for the
    reason `sessions` gives: a sweep that ignores it keeps ignoring it after
    somebody edits the registry.
    """
    if action is not Action.DELETE:
        raise ValueError(
            "the supervisor's record is pruned in place — there is no archive "
            "for an incident row to move into, and `actions` should have "
            "refused this at load"
        )

    (root,) = supervisor_incidents_roots()
    if not root.is_dir():
        return Swept()
    cutoff_day = date.today() - timedelta(days=keep_days)
    total = Swept()

    for path in sorted(root.glob("backend-stack-*.txt")):
        stamped = _compact_stamp_date(path.stem)
        if stamped is not None and stamped < cutoff_day:
            total += _retire(path, action, root / "archive")

    storms = root / "crash-storm-archive"
    if storms.is_dir():
        for path in sorted(storms.glob("*.json")):
            stamped = _compact_stamp_date(path.stem)
            if stamped is not None and stamped < cutoff_day:
                total += _retire(path, action, storms / "archive")

    incidents = root / "heartbeat-incidents.jsonl"
    if incidents.is_file():
        total += _pruned_rows(incidents, keep_days, "ts")
    return total


def _compact_stamp_date(stem: str) -> date | None:
    """The date out of a `...YYYYMMDDTHHMMSS...` stem, or `None`.

    The supervisor stamps its files with a compact instant rather than a dashed
    date (`backend-stack-11268-20260909T105351.108767Z`), so `_stamp_date` one
    function down reads nothing from them and would leave every dump in place
    for ever. Read from the LAST such run of digits, not the first: a pid is
    also a run of digits and a four-digit one would otherwise be parsed as the
    year.
    """
    for chunk in reversed(stem.replace("-", " ").replace("_", " ").split()):
        head = chunk.split("T")[0]
        if len(head) == 8 and head.isdigit():
            try:
                return date(int(head[:4]), int(head[4:6]), int(head[6:8]))
            except ValueError:
                return None
    return None


def consolidator_proposals_roots() -> tuple[Path, ...]:
    from tesseract.paths import log_dir

    return (log_dir("consolidator"),)


def consolidator_proposals(keep_days: int, action: Action) -> Swept:
    """`<logs>/consolidator/YYYY-MM-DD.jsonl` — what the feedback consolidator
    proposed on the day it ran.

    One file per run, rewritten rather than appended, holding every merge,
    soul edit and archive it suggested. The file is NOT what the operator acts
    on: `feedback_consolidator.py::_emit_inbox_events` puts each proposal on a
    workspace card carrying `keep`, `absorb`, `memory_id` and the reason
    inline, and passes the path only as provenance. So a card outlives its file
    and still works, which is what makes a window here safe at all.

    Aged on the name rather than the mtime, like every other dated artifact: an
    mtime makes a file that a backup touched look young forever.
    """
    (root,) = consolidator_proposals_roots()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for path in sorted(root.glob("*.jsonl")):
        stamped = _stamp_date(path.stem)
        if stamped is not None and stamped < cutoff:
            total += _retire(path, action, root / "archive")
    return total


def feedback_proposals_roots() -> tuple[Path, ...]:
    from tesseract.paths import log_dir

    return (log_dir("feedback-sweep"),)


def feedback_proposals(keep_days: int, action: Action) -> Swept:
    """`<logs>/feedback-sweep/YYYY-MM-DD.jsonl` — the memories the nightly
    sweep proposed from that day's conversations.

    The same shape and the same argument as `consolidator_proposals` one
    function up, and a separate sweep rather than a shared one because they are
    separate trees with separate windows: the consolidator reads the memory
    store and this reads transcripts, and a day on which one ran is not a day
    the other did. What the operator acts on is the card
    (`feedback_sweep.py::_emit_inbox_events`), not the file.
    """
    (root,) = feedback_proposals_roots()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for path in sorted(root.glob("*.jsonl")):
        stamped = _stamp_date(path.stem)
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


def receipts_roots() -> tuple[Path, ...]:
    from tesseract.orchestrator.turns.receipts import receipts_root

    return (receipts_root(),)


def receipts(keep_days: int, action: Action) -> Swept:
    """`runtime/receipts/YYYY-MM-DD.jsonl` — the marks each day's turns left.

    One file per day rather than a dated directory, so this sweeps files where
    `turn_manifests` sweeps directories, and it is dated by NAME for the same
    reason that one is: a backup touching a file would make an old record look
    young forever.

    **The window has to match `turn_manifests`.** A receipt outliving its step
    is a mark nobody can attribute; a step outliving its receipt is a claim
    that lost its evidence. `retention.yaml` says so beside both numbers.
    """
    (root,) = receipts_roots()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for path in sorted(root.glob("*.jsonl")):
        try:
            stamped = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if stamped >= cutoff:
            continue
        total += _retire(path, action, root / "archive")
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


def tool_results_roots() -> tuple[Path, ...]:
    from tesseract.brain.tool_spill import spill_root

    return (spill_root(),)


def tool_results(keep_days: int, action: Action) -> Swept:
    """`runtime/tool-results/YYYY-MM-DD/<call>.txt`: the whole of each tool
    result too long to send, in a directory named for the day it was saved.

    Aged on the directory name, for the reason `turn_manifests` is: a backup
    touching a file would make an old one look young forever.
    """
    from tesseract.lib import clock

    (root,) = tool_results_roots()
    if not root.is_dir():
        return Swept()
    cutoff = clock.today() - timedelta(days=keep_days)
    total = Swept()
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            stamped = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if stamped >= cutoff:
            continue
        for path in sorted(day_dir.glob("*.txt")):
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


def workspace_events_roots() -> tuple[Path, ...]:
    from tesseract.paths import home_logs_root

    root = home_logs_root() / "workspace"
    return (root / "events.jsonl", root / "comments.jsonl")


def workspace_events(keep_days: int, action: Action) -> Swept:
    """`<logs>/workspace/events.jsonl`, and the comments on what goes.

    The prune is `EventStore.prune_settled_before`, on the class that owns the
    file and both of its locks, for the same reason the usage ledger's and the
    approval ledger's are: a card filed between the read and the replace would
    land in neither.

    It ages an EVENT rather than a row, and only one that has been settled: an
    older row is that card's history, and dropping a `resolved` row would leave
    the `pending` one under it as the newest, putting an answered decision back
    in front of the operator.
    """
    from tesseract.paths import home_logs_root
    from tesseract.workspace_events import EventStore

    if action is not Action.DELETE:
        raise ValueError(
            "the inbox is pruned in place — there is no archive for it, "
            "because what an answered card changed is recorded where the "
            "change landed"
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    return Swept(removed=EventStore(home_logs_root()).prune_settled_before(cutoff))


def checkpoints_roots() -> tuple[Path, ...]:
    from tesseract.orchestrator.checkpoints import checkpoint_dir

    return (checkpoint_dir(),)


def checkpoints(keep_days: int, action: Action) -> Swept:
    """`<home>/checkpoints/<chat_id>.jsonl` — what each boundary wrote down.

    Aged on the NEWEST ROW, because the file is a conversation and not a day.
    The name carries no date to age on any more, and an mtime makes a file a
    backup touched look young forever. The last line is the newest: the file
    is append-only.

    A file whose last line will not parse, or carries no timestamp, is left
    alone. Deleting a conversation's only record because one line is truncated
    is the wrong way round.
    """
    (root,) = checkpoints_roots()
    if not root.is_dir():
        return Swept()
    cutoff = date.today() - timedelta(days=keep_days)
    total = Swept()
    for path in sorted(root.glob("*.jsonl")):
        stamped = _newest_row_date(path)
        if stamped is not None and stamped < cutoff:
            total += _retire(path, action, root / "archive")
    return total


def _newest_row_date(path: Path) -> date | None:
    """The date on the NEWEST row of a JSONL file, or `None`.

    The newest row and not the newest one that parses. Falling through to an
    older row when the last line is truncated is how a file gets aged on a
    date it has already moved past: a crash-truncated newest row is exactly
    the case the caller says it leaves alone, and reading past it deleted a
    file whose real last write was today.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            stamp = json.loads(line).get("ts")
        except (ValueError, AttributeError):
            return None
        if not isinstance(stamp, str):
            return None
        try:
            return datetime.fromisoformat(stamp).date()
        except ValueError:
            return None
    return None


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
    "agent_invocations",
    "agent_invocations_roots",
    "approvals_ledger",
    "approvals_ledger_roots",
    "backend_logs",
    "backend_logs_roots",
    "checkpoints",
    "checkpoints_roots",
    "conscience",
    "conscience_roots",
    "cost_ledger",
    "cost_ledger_roots",
    "consolidator_proposals",
    "consolidator_proposals_roots",
    "feedback_proposals",
    "feedback_proposals_roots",
    "janitor_sweeps",
    "janitor_sweeps_roots",
    "lane_archives",
    "lane_archives_roots",
    "loop_stalls",
    "loop_stalls_roots",
    "observer_logs",
    "observer_logs_roots",
    "scheduler_runs",
    "scheduler_runs_roots",
    "session_journal",
    "session_journal_roots",
    "sessions",
    "sessions_roots",
    "supervisor_incidents",
    "supervisor_incidents_roots",
    "receipts",
    "receipts_roots",
    "skill_usage",
    "skill_usage_roots",
    "tokenjuice_audit",
    "tokenjuice_audit_roots",
    "turn_manifests",
    "turn_manifests_roots",
    "usage_ledger",
    "usage_ledger_roots",
    "watchman_reports",
    "watchman_reports_roots",
]
