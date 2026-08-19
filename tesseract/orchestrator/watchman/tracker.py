"""`home/autonomy/WHAT-RUNS.md` — what runs on this machine, and whether it ran.

The operator's shape: two sections, one line each. *"These are the default
schedules. Check if everything ran. Create a report. Then the user, when he
creates his own, these are amended under."*

**Derived, never authored.** A hand-maintained file of this would be the third
instance of the defect this folder keeps finding — `HEARTBEAT.md` named a
cadence that had been deleted, `WHAT_NOT_TO_SAVE.md` claims to enable eleven
categories and enables none. So the default half is the manifest, which already
carries each entry's one-liner, and the operator's half is their own
`home/config/schedule.yaml`. Both sections derive; adding a row adds a line with
no edit anywhere.

It lives beside the watchman's reports rather than in `workspace/`: every file
there is in `PROPOSABLE_PATHS`, so `propose_change` would offer to edit a
generated file — which is exactly how `HEARTBEAT.md` came to look like
configuration. `Guide/reference/what-runs.md` is the other artifact from the
same manifest, built at release time, and structurally cannot carry a
per-machine row or a last-run time.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from tesseract.orchestrator.watchman.report import md_safe
from tesseract.orchestrator.watchman.rows import RowReport, RowState

log = logging.getLogger(__name__)

TRACKER_FILENAME = "WHAT-RUNS.md"

_HEADER = """\
# What runs on this machine

Everything here runs on its own, without being asked. The first section is what
the app ships; the second is what you have added. Both are derived on every
watchman pass — from the schedule, the run manifest and the run log — so a row
that appears in any of them appears here, and nothing in this file is written
by hand.

Read at {read_at}.
"""

_DEFAULT_INTRO = """\
## Schedules — Default

What the app ships. What each one is FOR is the app's and comes from the run
manifest; WHEN it fires is yours, in `home/config/schedule.yaml`.
"""

_YOURS_INTRO = """\
## Schedules — Yours

Rows you added. They fire exactly as the shipped ones do and are reported here
the same way; what they are for is yours to say.
"""

_COLUMNS = (
    "| Row | What it does | Fires | Last run | Outcome |\n"
    "| --- | --- | --- | --- | --- |"
)

_NO_SUMMARY = "_(no summary — add a `summary:` line and it appears here)_"


def tracker_path() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / "autonomy" / TRACKER_FILENAME


def _when(moment: datetime | None) -> str:
    """Local time, because the operator's cron is local and so is their day."""
    if moment is None:
        return "never"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _outcome(row: RowState) -> str:
    """One cell, and the worst true thing goes in it.

    A row can be several of these at once — disabled and stale, late and last
    seen failing — and a cell that lists all of them stops being a line the
    operator can scan.
    """
    if not row.enabled:
        return "off — you disabled it"
    if row.late_by is not None:
        hours = row.late_by.total_seconds() / 3600
        return (
            "**has never fired**" if row.never_ran
            else f"**late** — {hours:.1f}h past its next fire"
        )
    if row.never_ran:
        return "not yet"
    return row.last_outcome or "ran"


def _table(rows: tuple[RowState, ...]) -> list[str]:
    lines = [_COLUMNS]
    for row in rows:
        # A name and a summary are what somebody TYPED. Everything else in the
        # row the runtime derived, and only these two can carry a newline into
        # a file the assistant reads back as its own account of the machine.
        summary = md_safe(row.summary.strip()) or _NO_SUMMARY
        lines.append(
            f"| `{md_safe(row.name, limit=80)}` | {summary} | {row.fires} "
            f"| {_when(row.last_run)} | {_outcome(row)} |"
        )
    return lines


def render(report: RowReport, *, read_at: datetime) -> str:
    default = tuple(r for r in report.rows if r.declared)
    yours = tuple(r for r in report.rows if not r.declared)

    lines = [_HEADER.format(read_at=_when(read_at)), _DEFAULT_INTRO]
    if default:
        lines += _table(default)
    else:
        lines.append("Nothing shipped is armed on this machine.")
    lines += ["", _YOURS_INTRO]
    if yours:
        lines += _table(yours)
    else:
        lines.append(
            "You have not added one yet. Ask for a schedule in chat, or add a row "
            "to `home/config/schedule.yaml`, and it appears here on the next pass."
        )
    if not report.log_present:
        lines += [
            "",
            "Nothing has run on this machine yet — there is no run log to read, "
            "which is not the same as every row having stopped.",
        ]
    return "\n".join(lines).rstrip() + "\n"


def write(report: RowReport, *, read_at: datetime) -> Path:
    path = tracker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render(report, read_at=read_at), encoding="utf-8")
    tmp.replace(path)
    return path


def refresh(*, now: datetime, window_start: datetime | None) -> Path | None:
    """Re-derive the tracker. Returns `None` when it could not be, and the
    watchman's own pass carries on either way — a report that was written is
    worth more than a file that describes it."""
    from tesseract.orchestrator.watchman.rows import read_rows

    try:
        report = read_rows(now=now, window_start=window_start)
        return write(report, read_at=now)
    except Exception:  # noqa: BLE001 — a derived file must not cost the sweep
        log.exception("watchman: could not derive %s", TRACKER_FILENAME)
        return None


__all__ = ["TRACKER_FILENAME", "refresh", "render", "tracker_path", "write"]
