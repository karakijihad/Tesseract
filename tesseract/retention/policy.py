"""What ages, how long it is kept, and what happens to it then.

Four policies lived in three files before this: two `schedule.yaml` config
blocks, two `janitor.yaml` keys, and — for the approval ledger — nothing at
all. Nothing named them together, so nobody could answer "what does this
machine throw away" without reading four call sites, and the one tree with no
policy was the one where the absence mattered most.

**Code owns the catalog and the sweep; config owns the window.** The same line
`janitor.yaml` draws for its kill rule and AR-10 draws for the capture filter:
a window is a number an operator should be able to change, and *what a sweep
does to a file* is not. So `keep_days` and `action` come from
`config/retention.yaml`, and the key, the prose, the sweep and — decisively —
whether deleting is even permitted come from here.

`may_delete=False` is the reason this is a registry and not a dict of ints. A
security ledger that can be pruned by editing a yaml value is a security ledger
with no policy, only a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import yaml

MIN_SUMMARY_CHARS = 20
MIN_WHY_CHARS = 30


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

    def __add__(self, other: "Swept") -> "Swept":
        return Swept(
            self.moved + other.moved,
            self.removed + other.removed,
            self.failed + other.failed,
        )


# A sweep is given its resolved window and action and reports what it did. It
# never decides either — that is what makes the table the single place a
# policy is stated.
Sweep = Callable[[int, Action], Swept]


@dataclass(frozen=True)
class Tree:
    """One thing that ages, and this system's account of it."""

    key: str
    summary: str
    why: str
    sweep: Sweep
    # Whether `action: delete` may be declared for it at all. False is a
    # refusal at load, not a silent downgrade: an operator who wrote `delete`
    # asked for something, and quietly archiving instead would leave them
    # believing the file is gone.
    may_delete: bool = True

    def __post_init__(self) -> None:
        for field, text, floor in (
            ("summary", self.summary, MIN_SUMMARY_CHARS),
            ("why", self.why, MIN_WHY_CHARS),
        ):
            if len(text.strip()) < floor:
                raise RetentionError(
                    f"retention tree {self.key!r}: {field} is "
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


def _registry() -> dict[str, Tree]:
    # Imported here rather than at module import: `sweeps` reaches into the
    # session store and the observer, and a cycle through either would make
    # this file unimportable from the config loader that has to read it.
    from tesseract.retention import sweeps

    trees = (
        Tree(
            key="observer_logs",
            summary="The per-turn record of what the observer looked at.",
            why=(
                "One line is written every time the assistant watches a turn, "
                "so with no ceiling it becomes the largest thing on disk."
            ),
            sweep=sweeps.observer_logs,
        ),
        Tree(
            key="sessions",
            summary="Conversations in the live session drawer.",
            why=(
                "The drawer would hold every conversation ever held and get "
                "slower to open with each one. Archived, never deleted."
            ),
            sweep=sweeps.sessions,
            # The archive is the point of the window; deleting a conversation
            # is the operator's own act, through the app.
            may_delete=False,
        ),
        Tree(
            key="lane_archives",
            summary="Finished delegation lanes, kept after the work shipped.",
            why=(
                "Every delegated task leaves a transcript directory behind and "
                "nothing else ever removes one."
            ),
            sweep=sweeps.lane_archives,
        ),
        Tree(
            key="backend_logs",
            summary="One log file per backend launch.",
            why=(
                "A file is written every time the app starts, so the set grows "
                "with launches forever rather than with use."
            ),
            sweep=sweeps.backend_logs,
        ),
        Tree(
            key="scheduler_runs",
            summary="One row for every background job this machine has run.",
            why=(
                "It grows with uptime and is read whole every night. Its rows "
                "are already summarised into a daily rollup, so what a window "
                "removes is detail behind a record that stays."
            ),
            sweep=sweeps.scheduler_runs,
        ),
        Tree(
            key="watchman_reports",
            summary="The watchman's sweep reports and the evidence behind them.",
            why=(
                "A report and a file per finding are written every hour, so the "
                "set grows with uptime and nothing else removes one. The latest "
                "sweep is a separate pointer and is never aged."
            ),
            sweep=sweeps.watchman_reports,
        ),
        Tree(
            key="approvals_ledger",
            summary="Every permission decision the runtime has made.",
            why=(
                "It is the record an investigation reads. Rows removed to save "
                "disk are rows unavailable to the next forensic question, so "
                "old rows move to a dated file beside it and are never dropped."
            ),
            sweep=sweeps.approvals_ledger,
            may_delete=False,
        ),
        Tree(
            key="usage_ledger",
            summary="One row per tool call — which tool, in which session.",
            why=(
                "It is what makes the working set a measurement rather than a "
                "preference, and it answers questions about a window rather "
                "than about a year. It carries no inputs and no outputs, so an "
                "old row holds nothing a new one does not."
            ),
            sweep=sweeps.usage_ledger,
        ),
    )
    return {t.key: t for t in trees}


TREES: dict[str, Tree] = _registry()


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
        if not isinstance(keep_days, int) or isinstance(keep_days, bool) or keep_days < 1:
            problems.append(
                f"{key!r}: keep_days is {keep_days!r} — give it a whole number of "
                "days ≥ 1. To keep something forever, remove its row from the "
                "sweep rather than setting a window it can never reach"
            )
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


__all__ = [
    "Action",
    "Policy",
    "RetentionError",
    "Swept",
    "TREES",
    "Tree",
    "load_live",
    "load_policies",
]
