"""`home/autonomy/WHAT-RUNS.md` — what runs on this machine, and whether it ran.

The operator's shape: every row on one line, and a tag saying whether the app
brought it. *"These are the default schedules. Check if everything ran. Create
a report. Then the user, when he creates his own, these are amended under."*
It was two sections once, and the tag replaced them: two sections AND a tag
would be two mechanisms answering one question.

**Derived, never authored.** A hand-maintained file of this would be the third
instance of the defect this folder keeps finding — `HEARTBEAT.md` named a
cadence that had been deleted, `WHAT_NOT_TO_SAVE.md` claims to enable eleven
categories and enables none. So the default half is the manifest, which already
carries each entry's one-liner, and the operator's half is their own
`home/config/schedule.yaml`. Both sections derive; adding a row adds a line with
no edit anywhere.

It carries the agents on the same pass and in the same shape (operator,
2026-08-17: *"this will also be applied to the same thing to the agents… what
agents do we have? Yours and default"*). Their default/yours split is already a
location — shipped cards in `app/`, the operator's in `home/` — and
what a card says about itself is its own `description`, so that section derives
exactly as the schedules do.

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
from typing import TYPE_CHECKING

from tesseract.agents.invocations import COUNT_WINDOW_DAYS
from tesseract.orchestrator.watchman.report import md_safe
from tesseract.orchestrator.watchman.rows import RowReport, RowState

if TYPE_CHECKING:
    from tesseract.agents.roster import CardRow

log = logging.getLogger(__name__)

TRACKER_FILENAME = "WHAT-RUNS.md"

_HEADER = """\
# What runs on this machine

Everything here runs on its own, without being asked. A `built-in` one came
with the app. A `custom` one was added here, either by you or by the assistant
at your request, and both count as yours to change.

Every line is derived on each watchman pass, from the schedule, the run
manifest, the agent cards and the run log. A row that appears in any of them
appears here, and nothing in this file is written by hand.

Read at {read_at}.
"""

#: Two words for one question: did the app bring this, or did it get added
#: here. Everything added is `custom`, whether a person wrote it by hand or
#: asked the assistant for it, because they approved it either way and it is
#: theirs either way. The words are the same on every surface that shows the
#: split (operator, 2026-08-26), which is why they live in one place.
TAG_BUILT_IN = "built-in"
TAG_CUSTOM = "custom"

_SCHEDULES_INTRO = """\
## Schedules

A `built-in` row came with the app: what it is FOR is the app's and comes from
the run manifest, while WHEN it fires and WHERE it reports are yours, in
`home/config/schedule.yaml`. A `custom` row was added here, by you or by the
assistant at your request; it fires exactly as a built-in one does, and what
it is for is yours to say.
"""

_COLUMNS = (
    "| Row | Tag | What it does | Fires | Reports to | Last run | Outcome |\n"
    "| --- | --- | --- | --- | --- | --- | --- |"
)

# A row that named no channels follows the kind's own routing, which is
# what every row starts as. Saying so beats an empty cell: the operator
# reading this is deciding whether to change it, and a blank looks like a
# gap.
_ROUTED_BY_KIND = "where that message usually goes"

_NO_SUMMARY = "_(no summary — add a `summary:` line and it appears here)_"

_AGENTS_INTRO = """\
## Agents

A `built-in` card came with the app; a `custom` one was added under
`home/agents/`, by you or by the assistant at your request, and a custom card
taking a shipped name covers it. What each is FOR is the card's own
`description`; whether anything ever calls it is the last column, and a card
nothing calls is the thing this table exists to make visible.
"""

_AGENT_COLUMNS = (
    "| Agent | Tag | What it does | Runs on | Last invoked |\n"
    "| --- | --- | --- | --- | --- |"
)

_NO_DESCRIPTION = "_(no description — add one and it appears here)_"


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
        # A channel name is what somebody TYPED, and the routing table
        # does not refuse one it has no adapter for, so it is escaped
        # like the two above rather than trusted like the derived cells.
        reports = md_safe(row.reports_to, limit=80) or _ROUTED_BY_KIND
        tag = TAG_BUILT_IN if row.declared else TAG_CUSTOM
        lines.append(
            f"| `{md_safe(row.name, limit=80)}` | {tag} | {summary} | {row.fires} "
            f"| {reports} | {_when(row.last_run)} | {_outcome(row)} |"
        )
    return lines


def _last_invoked(row: "CardRow") -> str:
    """One cell, and never-invoked is a state rather than a blank.

    A card that has run says when and how often lately; a card that has not
    says so in words, because an empty cell reads as "not measured" and this
    one is measured.
    """
    if row.disabled:
        return "off — you disabled it"
    if row.last is None:
        return "**never**"
    lately = f", {row.last.count}x in {COUNT_WINDOW_DAYS}d" if row.last.count else ""
    via = f" via `{md_safe(row.last.via, limit=40)}`" if row.last.via else ""
    return f"{_when(row.last.last_at)}{lately}{via}"


def _agent_table(rows: "tuple[CardRow, ...]") -> list[str]:
    lines = [_AGENT_COLUMNS]
    for row in rows:
        # `description` is what somebody typed, so it goes through the same
        # guard a row summary does — a pipe or a newline in it would otherwise
        # end the table the assistant reads back as its account of itself.
        # Tighter than a row summary's 300: a card's `description` is written
        # for its own card, where it has a paragraph, and at full length it
        # turns a scannable table into six wrapped lines per card.
        description = md_safe(row.description, limit=160) or _NO_DESCRIPTION
        # What a card says it is for matters less than a card that cannot run
        # at all, so the verdict takes the cell. A shipped card in this state
        # stops the boot, which leaves the operator's own: reported here and
        # in the Managed system room, because the report used to be one log
        # line the app does not forward.
        if row.cannot_run:
            description = f"**Cannot run.** {md_safe(row.cannot_run, limit=200)}"
        name = md_safe(row.name, limit=80)
        shadow = " _(covers a shipped card)_" if row.shadows_system else ""
        tag = TAG_BUILT_IN if row.origin == "system" else TAG_CUSTOM
        lines.append(
            f"| `{name}`{shadow} | {tag} | {description} "
            f"| `{md_safe(row.model_role, limit=40)}` | {_last_invoked(row)} |"
        )
    return lines


def _agent_sections(read_at: datetime) -> list[str]:
    """The agents half. One file answers "what runs on this machine", and an
    operator who has to open a second one to find out is reading a tracker
    that only half exists."""
    from tesseract.agents.roster import read_roster

    # Built-in first, then custom, so the tag column reads in blocks rather
    # than alternating. The ORDER is a convenience; the tag is what says which
    # is which, and a reader scanning for their own card finds it either way.
    rows = sorted(
        read_roster(now=read_at), key=lambda r: (r.origin != "system", r.name),
    )
    lines = ["", _AGENTS_INTRO]
    if rows:
        lines += _agent_table(tuple(rows))
    else:
        lines.append("There are no agent cards on this machine.")
    if not any(r.origin != "system" for r in rows):
        lines.append("")
        lines.append(
            "You have added none of your own yet. Ask for an agent in chat, or "
            "drop a card into `home/agents/`, and it appears here with a "
            f"`{TAG_CUSTOM}` tag on the next pass."
        )
    return lines


def render(report: RowReport, *, read_at: datetime) -> str:
    # One table, and the tag carries the split. Two sections plus a tag would
    # be two mechanisms answering one question, which is the shape of defect
    # this file exists to report rather than commit.
    rows = sorted(report.rows, key=lambda r: (not r.declared, r.name))

    lines = [_HEADER.format(read_at=_when(read_at)), _SCHEDULES_INTRO]
    if rows:
        lines += _table(tuple(rows))
    else:
        lines.append("Nothing is armed on this machine.")
    if not any(not r.declared for r in rows):
        lines.append("")
        lines.append(
            "You have added none of your own yet. Ask for a schedule in chat, or "
            "add a row to `home/config/schedule.yaml`, and it appears here with "
            f"a `{TAG_CUSTOM}` tag on the next pass."
        )
    try:
        lines += _agent_sections(read_at)
    except Exception:  # noqa: BLE001 — the schedules half is worth keeping alone
        log.exception("watchman: could not derive the agents half of %s", TRACKER_FILENAME)
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
