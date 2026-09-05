"""What ages, how long it is kept, and what happens to it then.

Four policies lived in three files before this: two `schedule.yaml` config
blocks, two `janitor.yaml` keys, and — for the approval ledger — nothing at
all. Nothing named them together, so nobody could answer "what does this
machine throw away" without reading four call sites, and the one tree with no
policy was the one where the absence mattered most.

**Code owns the catalog and the sweep; config owns the window.** The same
line `janitor.yaml` draws for its kill rule, and the capture filter for
its own:
a window is a number an operator should be able to change, and *what a sweep
does to a file* is not. So `keep_days` and `action` come from
`config/retention.yaml`, and the key, the prose, the sweep and — decisively —
whether deleting is even permitted come from here.

`may_delete=False` is the reason this is a registry and not a dict of ints. A
security ledger that can be pruned by editing a yaml value is a security ledger
with no policy, only a default.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import yaml

from tesseract.lib.yaml_io import round_trip_yaml

MIN_SUMMARY_CHARS = 20
MIN_WHY_CHARS = 30
# A name a person reads, not a key. Short, because it is the first column of a
# row, and never the key with the underscores taken out: `scheduler_runs` is a
# file-tree name and the panel's rule is that nothing prints a name only this
# repo understands.
MIN_TITLE_CHARS = 6
# The shortest window anything may be given. A window of zero days is not
# a window, it is a request to delete on sight, and that is a different
# act from ageing.
MIN_KEEP_DAYS = 1

# Where a tree lives, resolved when it is asked for. Every root comes from
# `sweeps.py`, beside the function that ages it, so the panel measures exactly
# what the sweep walks.
Roots = Callable[[], tuple[Path, ...]]


def why_not_a_window(value: object, floor: int = MIN_KEEP_DAYS) -> str | None:
    """Why `value` cannot be a window for a tree with this floor, or `None`.

    One rule, two doors. The loader reads a hand-edited file and the window
    control writes one, and a control that accepts what the next boot refuses
    is a control that breaks the machine quietly. Everything a caller shows a
    person comes back from here, so both doors refuse in the same words.

    `floor` is the tree's own, above the global one. Measured 2026-08-29, the
    first day a window could be set from the room: the permission ledger was
    taken to ten days by a live call, and the only thing that noticed was a
    test asserting thirty, in CI, after the fact. `may_delete=False` says the
    evidence must survive, and a window short enough to empty the live file is
    the same defect through a different door.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return (
            f"keep_days is {value!r}. It has to be a whole number of days, "
            "so nothing here is aged until it is one."
        )
    if value < MIN_KEEP_DAYS:
        return (
            f"keep_days is {value!r}. The shortest window is "
            f"{MIN_KEEP_DAYS} day. To keep something for good, take its row "
            "out of the sweep rather than giving it a window it can never "
            "reach."
        )
    if value < floor:
        return (
            f"keep_days is {value!r}, and the shortest window this one takes "
            f"is {floor} days. It is the record an investigation reads, and "
            "how much of it is in front of you is not a setting that goes to "
            "nothing."
        )
    return None


class RetentionError(ValueError):
    """A policy that does not hold. Raised at load, before any file is touched."""


class Action(str, Enum):
    DELETE = "delete"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class Swept:
    """What one sweep did. `moved` and `removed` are separate because the
    difference between them is the whole point of the table."""

    moved: int = 0
    removed: int = 0
    failed: int = 0
    # What the window reached and the sweep kept anyway, because removing it
    # would have left no record at all. Only `scheduler_runs` can hold rows
    # back today, and the count is what says the summarise-then-prune contract
    # is holding: a summary that silently did not happen is how a log becomes
    # the only record and then stops being one.
    held: int = 0

    def __add__(self, other: "Swept") -> "Swept":
        return Swept(
            self.moved + other.moved,
            self.removed + other.removed,
            self.failed + other.failed,
            self.held + other.held,
        )


# A sweep is given its resolved window and action and reports what it did. It
# never decides either — that is what makes the table the single place a
# policy is stated.
Sweep = Callable[[int, Action], Swept]


@dataclass(frozen=True)
class Tree:
    """One thing that ages, and this system's account of it."""

    key: str
    # What to call it on a screen. The key is the name of a directory or a
    # file; this is the name of the thing.
    title: str
    summary: str
    why: str
    sweep: Sweep
    where: Roots
    # Whether `action: delete` may be declared for it at all. False is a
    # refusal at load, not a silent downgrade: an operator who wrote `delete`
    # asked for something, and quietly archiving instead would leave them
    # believing the file is gone.
    may_delete: bool = True
    # What this tree's sweep can actually DO, which is a different question
    # from the one above. `may_delete` is policy: the sweep could delete and
    # is not allowed to. This is capability: `usage_ledger` prunes rows in
    # place and there is no archive for it to move them to, so `archive` is
    # not a decision it may take.
    #
    # Both are checked at load because `RetentionError` promises they are
    # ("Raised at load, before any file is touched"). Without this one, an
    # `action` the loader accepted raised inside the nightly sweep instead,
    # where the job records it per tree and carries on — so the table would
    # read as in force while that tree silently never aged again.
    actions: tuple[Action, ...] = (Action.DELETE, Action.ARCHIVE)
    # The shortest window THIS tree takes, where the global floor of one day
    # is not enough. Declared beside `may_delete` because it answers the same
    # question from the other side: that one says the evidence may not be
    # deleted, this one says how much of it stays in front of you.
    floor_days: int = MIN_KEEP_DAYS

    def __post_init__(self) -> None:
        _check_prose(
            self.key,
            ("title", self.title, MIN_TITLE_CHARS),
            ("summary", self.summary, MIN_SUMMARY_CHARS),
            ("why", self.why, MIN_WHY_CHARS),
        )


@dataclass(frozen=True)
class Kept:
    """One thing under the log trees that is deliberately NOT aged.

    A tree with no window is either a decision or an oversight, and from a
    directory listing the two look identical. Declaring the decisions is what
    lets the panel say which of the three a tree is: aged, kept on purpose, or
    nothing has decided about it yet. The last of those is the one worth
    surfacing — `logs/schedule/runs.jsonl` reached 868 KB in eleven days
    while five trees around it had windows and nobody noticed.

    There is no `sweep` and no `may_delete`, because nothing here is swept.
    """

    key: str
    title: str
    why: str
    where: Roots

    def __post_init__(self) -> None:
        _check_prose(
            self.key,
            ("title", self.title, MIN_TITLE_CHARS),
            ("why", self.why, MIN_WHY_CHARS),
        )


def _check_prose(key: str, *fields: tuple[str, str, int]) -> None:
    for field, text, floor in fields:
        if len(text.strip()) < floor:
            raise RetentionError(
                f"retention tree {key!r}: {field} is "
                f"{len(text.strip())} characters and the floor is {floor}"
            )


@dataclass(frozen=True)
class Policy:
    """A tree with the window and action config resolved onto it."""

    tree: Tree
    keep_days: int
    action: Action

    def run(self) -> Swept:
        return self.tree.sweep(self.keep_days, self.action)

    def in_words(self) -> str:
        """The window and what happens at it, in one sentence.

        Here rather than on the surface that shows it, because the room, the
        panel's written line and the control that changes it all say this and
        three sentences for one policy is how a machine ends up describing
        itself differently depending on where you look.

        `may_delete` is stated where it is true rather than shown as a missing
        option: an operator who cannot delete something should be told so, not
        left to infer it from a control that is not there.
        """
        span = "a day" if self.keep_days == 1 else f"{self.keep_days} days"
        if self.action is Action.ARCHIVE:
            at_the_end = "then it is moved aside and kept"
        else:
            at_the_end = "then it is deleted"
        if not self.tree.may_delete:
            at_the_end += ", and it may never be deleted from"
        return f"Kept {span}, {at_the_end}."


def _registry() -> dict[str, Tree]:
    # Imported here rather than at module import: `sweeps` reaches into the
    # session store and the observer, and a cycle through either would make
    # this file unimportable from the config loader that has to read it.
    from tesseract.retention import sweeps

    trees = (
        Tree(
            key="observer_logs",
            title="What the observer looked at",
            summary="The per-turn record of what the observer looked at.",
            why=(
                "One line is written every time the assistant watches a turn, "
                "so with no ceiling it becomes the largest thing on disk."
            ),
            sweep=sweeps.observer_logs,
            where=sweeps.observer_logs_roots,
        ),
        Tree(
            key="sessions",
            title="Your conversations",
            summary="Conversations in the live conversations rail.",
            why=(
                "The rail would hold every conversation ever held and get "
                "slower to open with each one. Archived, never deleted."
            ),
            sweep=sweeps.sessions,
            where=sweeps.sessions_roots,
            # The archive is the point of the window; deleting a conversation
            # is the operator's own act, through the app.
            may_delete=False,
        ),
        Tree(
            key="lane_archives",
            title="Finished delegations",
            summary="Finished delegation lanes, kept after the work shipped.",
            why=(
                "Every delegated task leaves a transcript directory behind and "
                "nothing else ever removes one."
            ),
            sweep=sweeps.lane_archives,
            where=sweeps.lane_archives_roots,
        ),
        Tree(
            key="worker_archives",
            title="Copies of the code a delegation worked in",
            summary=(
                "A copy of the whole project for every delegated task that "
                "edited code."
            ),
            why=(
                "Each one is a copy of the entire project, so a handful of "
                "finished tasks is more disk than everything else in this "
                "table put together. A task that did not finish cleanly keeps "
                "its copy whatever this window says, because that copy is the "
                "only place its work survives."
            ),
            sweep=sweeps.worker_archives,
            where=sweeps.worker_archives_roots,
            # The archive is already where a finished copy is moved TO, so
            # there is nowhere to archive one to.
            actions=(Action.DELETE,),
        ),
        Tree(
            key="backend_logs",
            title="One log per app launch",
            summary="One log file per backend launch.",
            why=(
                "A file is written every time the app starts, so the set grows "
                "with launches forever rather than with use."
            ),
            sweep=sweeps.backend_logs,
            where=sweeps.backend_logs_roots,
        ),
        Tree(
            key="scheduler_runs",
            title="Every background job it has run",
            summary="One row for every background job this machine has run.",
            why=(
                "It grows with uptime and is read whole every night. Its rows "
                "are already summarised into a daily rollup, so what a window "
                "removes is detail behind a record that stays."
            ),
            sweep=sweeps.scheduler_runs,
            where=sweeps.scheduler_runs_roots,
        ),
        Tree(
            key="watchman_reports",
            title="The watchman's reports",
            summary="The watchman's sweep reports and the evidence behind them.",
            why=(
                "A report and a file per finding are written every hour, so the "
                "set grows with uptime and nothing else removes one. The latest "
                "sweep is a separate pointer and is never aged."
            ),
            sweep=sweeps.watchman_reports,
            where=sweeps.watchman_reports_roots,
        ),
        Tree(
            key="conscience",
            title="How the runtime is trending",
            summary="The drift check's own record of how the runtime is trending.",
            why=(
                "A row per run of a check that runs on a clock. Only the latest "
                "row is ever read, because drift is a state, so a window removes "
                "history behind a verdict that stays."
            ),
            sweep=sweeps.conscience,
            where=sweeps.conscience_roots,
        ),
        Tree(
            key="approvals_ledger",
            title="Every permission decision",
            summary="Every permission decision the runtime has made.",
            why=(
                "It is the record an investigation reads. Rows removed to save "
                "disk are rows unavailable to the next forensic question, so "
                "old rows move to a dated file beside it and are never dropped."
            ),
            sweep=sweeps.approvals_ledger,
            where=sweeps.approvals_ledger_roots,
            may_delete=False,
            # A month of decisions in the live file. Below that the archive is
            # holding almost all of it, and answering "what did it allow last
            # week" means reading dated files rather than the ledger.
            floor_days=30,
        ),
        Tree(
            key="usage_ledger",
            title="Which tools get called",
            summary="One row per tool call — which tool, in which session.",
            why=(
                "It is what makes the working set a measurement rather than a "
                "preference, and it answers questions about a window rather "
                "than about a year. It carries no inputs and no outputs, so an "
                "old row holds nothing a new one does not."
            ),
            sweep=sweeps.usage_ledger,
            where=sweeps.usage_ledger_roots,
            # No archive exists for it, and the `why` above is the reason one
            # was never built. `archive` is a word this row cannot honour.
            actions=(Action.DELETE,),
        ),
        Tree(
            key="agenda_records",
            title="What autonomy decided, in full",
            summary="The complete record of every finished agenda item, and the index rows about it.",
            why=(
                "A finished item is a few kilobytes that grow with every "
                "transition and attempt, and the machine finishes several a "
                "day. What is still worth knowing about one afterwards, what it "
                "was for and how it ended, is a row in agenda/history/ that is "
                "written before the record goes and is never aged."
            ),
            sweep=sweeps.agenda_records,
            where=sweeps.agenda_records_roots,
            actions=(Action.DELETE,),
        ),
        Tree(
            key="turn_manifests",
            title="How each turn ran",
            summary="One record per conversation turn: its steps and how it ended.",
            why=(
                "A turn writes one small file and a person can hold dozens of "
                "conversations a day, so with no ceiling this grows with use. "
                "What it is for is answered within days: recovering a turn a "
                "restart cut short, and reading back what a turn actually did."
            ),
            sweep=sweeps.turn_manifests,
            where=sweeps.turn_manifests_roots,
        ),
        Tree(
            key="loop_stalls",
            title="How long the app was blocked",
            summary="One row per block of the event loop long enough to matter.",
            why=(
                "It is written only when it happens, so a healthy machine adds "
                "nothing for weeks. What it is for is comparing this week with "
                "last week, and a block from the spring explains nothing about "
                "today."
            ),
            sweep=sweeps.loop_stalls,
            where=sweeps.loop_stalls_roots,
        ),
    )
    return {t.key: t for t in trees}


def _kept() -> dict[str, Kept]:
    """The trees under the log roots that are deliberately never aged.

    These used to be a comment at the foot of `retention.yaml`, which meant a
    reader could see the decision and no surface could. A directory listing
    cannot tell a decision from an oversight, so the decisions are declared
    here and the panel subtracts both these and the swept trees from what it
    finds on disk. Whatever is left is the third answer: nothing has decided
    about it yet.
    """
    from tesseract.paths import (
        home_logs_root,
        log_dir,
        runtime_logs_root,
    )
    from tesseract.retention.record import record_path

    from tesseract.orchestrator.autonomy.agenda_history import history_dir

    kept = (
        Kept(
            key="agenda_history",
            title="Every task and decision, one line each",
            why=(
                "One compact row per finished agenda item, about two hundred "
                "bytes, written when the full record closes and again before "
                "the sweep deletes one. It is what lets a task finished long "
                "ago still be named and counted, so it is the one part of the "
                "agenda that does not age."
            ),
            where=lambda: (history_dir(),),
        ),
        Kept(
            key="audit_log",
            title="The permission audit trail",
            why=(
                "One line per event, written as it happens. It is the record "
                "an investigation reads and it is small enough that a window "
                "would cost more than it saves."
            ),
            where=lambda: (log_dir("audit"),),
        ),
        Kept(
            key="governor_pauses",
            title="When the governor stepped in",
            why=(
                "One row each time a run was paused, which is rare by design. "
                "A window over something that only grows when the machine is "
                "in trouble would remove exactly the history worth keeping."
            ),
            where=lambda: (log_dir("governor") / "pauses.jsonl",),
        ),
        Kept(
            key="repair_attempts",
            title="What the runtime fixed about itself",
            why=(
                "One line each time a declared repair ran, and it only writes "
                "when something was actually wrong. A window over a record "
                "that grows only when the machine is in trouble would remove "
                "exactly the history worth keeping, which is the argument the "
                "governor's pauses already make one row up."
            ),
            where=lambda: (log_dir("repairs"),),
        ),
        Kept(
            key="provider_health",
            title="Whether each provider answered",
            why=(
                "One row per probe, and the file is how a provider that has "
                "been failing all week is told apart from one that failed "
                "once. Small per event and read across days."
            ),
            where=lambda: (log_dir("provider-health"),),
        ),
        Kept(
            key="sweep_record",
            title="What the last sweep did",
            why=(
                "One file, overwritten every night, holding what each tree "
                "lost. It is a live pointer rather than a history, so it is "
                "the same size tomorrow as it is today."
            ),
            # The scratch name too. `write` renames onto the record, so the
            # half-written file exists for an instant, and one left behind by
            # a process that died mid-write would otherwise sit in this room's
            # own third band forever.
            where=lambda: (
                record_path(),
                record_path().with_suffix(".json.writing"),
            ),
        ),
        Kept(
            key="process_logs",
            title="The running app's own log",
            why=(
                "One aggregate file per process, bounded by size rotation "
                "rather than by a window. Ageing it by date as well would be "
                "two mechanisms holding one ceiling."
            ),
            where=lambda: tuple(
                root / name
                for root in (home_logs_root(), runtime_logs_root())
                for name in (
                    "mirror-backend.log",
                    "agent-controller.log",
                    "supervisor.log",
                )
            ),
        ),
    )
    return {k.key: k for k in kept}


TREES: dict[str, Tree] = _registry()
KEPT: dict[str, Kept] = _kept()


def load_policies(config_dir: Path) -> tuple[Policy, ...]:
    """Resolve `retention.yaml` against the registry, or raise saying why.

    Both directions are checked. A tree the code declares and the config omits
    would age on a default nobody wrote down; a key the config names and the
    code does not know is a policy the operator believes is in force and that
    nothing implements — the second being the exact defect that put
    `WHAT_NOT_TO_SAVE.md`'s eleven categories in a file where eight were real.
    """
    path = config_dir / "retention.yaml"
    if not path.exists():
        raise RetentionError(f"retention: {path} does not exist")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RetentionError(f"retention: {path} must be a mapping")
    trees_raw = raw.get("trees")
    if not isinstance(trees_raw, dict):
        raise RetentionError("retention: `trees` must be a mapping of key → policy")

    problems: list[str] = []
    for key in sorted(set(trees_raw) - set(TREES)):
        problems.append(
            f"{key!r} is configured and nothing implements it — add a Tree in "
            "retention/policy.py or remove the row"
        )
    for key in sorted(set(TREES) - set(trees_raw)):
        problems.append(
            f"{key!r} is implemented and configured nowhere — it would age on a "
            "window no one wrote down"
        )

    policies: list[Policy] = []
    for key, tree in sorted(TREES.items()):
        block = trees_raw.get(key)
        if not isinstance(block, dict):
            if key in trees_raw:
                problems.append(f"{key!r}: policy must be a mapping")
            continue
        keep_days = block.get("keep_days")
        refusal = why_not_a_window(keep_days, tree.floor_days)
        if refusal is not None:
            problems.append(f"{key!r}: {refusal}")
            continue
        try:
            action = Action(str(block.get("action")))
        except ValueError:
            problems.append(
                f"{key!r}: action is {block.get('action')!r} — one of "
                f"{[a.value for a in Action]}"
            )
            continue
        if action is Action.DELETE and not tree.may_delete:
            problems.append(
                f"{key!r}: action is 'delete' and this tree may not be deleted "
                f"from. {tree.why.strip()}"
            )
            continue
        if action not in tree.actions:
            problems.append(
                f"{key!r}: action is {action.value!r} and this tree's sweep "
                f"only does {', '.join(a.value for a in tree.actions)}. "
                f"{tree.why.strip()}"
            )
            continue
        policies.append(Policy(tree=tree, keep_days=keep_days, action=action))

    if problems:
        raise RetentionError(
            "the retention table does not describe what this machine does:\n  - "
            + "\n  - ".join(problems)
        )
    return tuple(policies)


def load_live() -> tuple[Policy, ...]:
    """`load_policies` against the config tree this install runs from."""
    from tesseract.paths import config_dir

    return load_policies(config_dir())


def resolve_tree(name: str) -> Tree | None:
    """The tree a person meant, by its key or by the name it is shown under.

    Both, because the two callers see different things. A room hands back the
    key it was given; a person on a phone has only ever seen "Every background
    job it has run", and `scheduler_runs` is a name only this repo understands.
    """
    wanted = " ".join(name.split()).casefold()
    for tree in TREES.values():
        if wanted in (tree.key.casefold(), tree.title.casefold()):
            return tree
    return None


def what_can_be_windowed() -> str:
    """Every tree a window may be set on, as one line a person can act on."""
    return ", ".join(
        f"{tree.title} ({tree.key})" for tree in sorted(TREES.values(), key=lambda t: t.key)
    )


#: Held across the whole read, write and re-read of the table.
#:
#: The write is a round trip: the file is parsed, one scalar is changed, and
#: the WHOLE document is written back. Two of those overlapping lose one of
#: them silently, because the second was parsed before the first landed and
#: carries every other row at its old value. That is not a theoretical race
#: here: this control exists so the same window can be changed from the room
#: and from a phone, which is two turns and two sessions reaching one file.
#:
#: One process holds one lock, so a hand edit made in an editor at the same
#: moment is still outside it. Nothing can close that, and the re-read below
#: is what makes it safe rather than silent: the policy handed back is read
#: off the file afterwards, so a caller is never told a window is in force
#: that is not.
_writing = threading.Lock()


def set_window(config_dir: Path, name: str, keep_days: int) -> tuple[Policy, Policy]:
    """Give one tree a new window. Returns what it was and what it now is.

    The window is the one thing about a sweep that is the operator's, so it is
    the one thing this changes: what a sweep DOES to a file stays in the code,
    for the reason `may_delete` exists at all.

    Everything it refuses, it refuses in the words the loader would use at the
    next boot, because a control that accepts what the file refuses leaves a
    machine that will not start. It reads the table before writing, so a table
    that does not hold is reported as that rather than half-repaired, and reads
    it again after, so nothing is reported as set that the next sweep would
    reject.

    The comments in `retention.yaml` are what explains every row to whoever
    opens it, so the write is a round trip rather than a dump. Reading and
    writing are one act under `_writing`, because a round trip that parses
    before another one writes puts every row back as it found it.
    """
    with _writing:
        return _set_window(config_dir, name, keep_days)


def _set_window(config_dir: Path, name: str, keep_days: int) -> tuple[Policy, Policy]:
    before = {policy.tree.key: policy for policy in load_policies(config_dir)}
    tree = resolve_tree(name)
    if tree is None:
        raise RetentionError(
            f"nothing here is called {name!r}, so no window was changed. "
            f"What has one: {what_can_be_windowed()}"
        )
    refusal = why_not_a_window(keep_days, tree.floor_days)
    if refusal is not None:
        raise RetentionError(f"{tree.title}: {refusal}")
    was = before[tree.key]
    if was.keep_days == keep_days:
        return was, was

    def _apply(doc: Any) -> None:
        doc["trees"][tree.key]["keep_days"] = keep_days

    round_trip_yaml(config_dir / "retention.yaml", _apply)

    after = {policy.tree.key: policy for policy in load_policies(config_dir)}
    return was, after[tree.key]


__all__ = [
    "Action",
    "KEPT",
    "Kept",
    "Policy",
    "RetentionError",
    "Roots",
    "Swept",
    "TREES",
    "Tree",
    "MIN_KEEP_DAYS",
    "load_live",
    "load_policies",
    "resolve_tree",
    "set_window",
    "what_can_be_windowed",
    "why_not_a_window",
]
