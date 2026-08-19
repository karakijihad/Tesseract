"""Does the manifest describe the machine that is actually about to run?

The manifest ships BESIDE `schedule.yaml` before it replaces it, so the only
thing standing between a declaration and a fiction is this file. It raises,
where `pipeline/checks.py` reports: those checks cover config this repo does not
own the other half of, and three of them fail on a healthy tree today. These
cover a declaration this code owns entirely, and a row nobody declared is not a
degraded state to run in — it is the state the manifest exists to make
impossible.

Only SHIPPED rows are checked. A row the operator wrote lives in their own
`home/config/schedule.yaml`, is theirs by the ownership boundary, and owes this
file nothing; the floor plan shows those in its own pane.

What is checked here is what can be checked from yaml alone, because this runs
while the scheduler is arming. The cross-check that needs the handler classes —
a job whose `default_model_role` resolves to a chain the entry does not name —
is a test, where importing thirteen task modules costs nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tesseract.scheduler.manifest.entry import (
    DISPATCHED,
    Entry,
    Kind,
    ManifestError,
    Runs,
)
from tesseract.scheduler.manifest.registry import ENTRIES, entries_of


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ManifestError(f"manifest check: {path} does not exist")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest check: {path} must be a mapping")
    return raw


def _row_roles(row: dict[str, Any]) -> set[str]:
    """Every role name this row's own config names — its own, and its stages'."""
    roles: set[str] = set()
    row_role = row.get("model_role")
    if isinstance(row_role, str) and row_role.strip():
        roles.add(row_role.strip())
    config = row.get("config")
    if isinstance(config, dict):
        for block in config.values():
            if isinstance(block, dict):
                stage_role = block.get("model_role")
                if isinstance(stage_role, str) and stage_role.strip():
                    roles.add(stage_role.strip())
    return roles


def check_rows(schedule: dict[str, Any]) -> list[str]:
    """Shipped rows and their entries name the same set, in both directions —
    and agree on how each one fires.

    A row that declares `when:` is a `trigger` entry and a row that declares
    `cadence:` is a `row` entry. Checking the kind rather than only the name is
    what stops the manifest from saying "hourly" about work that waits for an
    event: the floor plan renders that word, and an entry is the only place it
    is written down.
    """
    shipped: dict[str, Runs] = {}
    for row in schedule.get("jobs") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name:
            continue
        shipped[name] = Runs.TRIGGER if str(row.get("when") or "").strip() else Runs.ROW
    declared = {
        e.name: e.runs
        for e in (*entries_of(Runs.ROW), *entries_of(Runs.TRIGGER))
    }
    problems = []
    for name in sorted(set(shipped) - set(declared)):
        problems.append(
            f"row {name!r} ships in schedule.yaml and is in no manifest entry — "
            "say what it does and what would be lost without it, or delete the row"
        )
    for name in sorted(set(declared) - set(shipped)):
        problems.append(
            f"manifest declares {declared[name].value} {name!r} and schedule.yaml "
            "ships no such row"
        )
    for name in sorted(set(shipped) & set(declared)):
        if shipped[name] is declared[name]:
            continue
        fires = "an event" if shipped[name] is Runs.TRIGGER else "a clock"
        problems.append(
            f"row {name!r} fires on {fires} and the manifest declares it a "
            f"{declared[name].value}"
        )
    return problems


def check_chains(roles_raw: dict[str, Any], entries: tuple[Entry, ...]) -> list[str]:
    """Every chain an entry names exists in `roles.yaml::chains`."""
    known = set(roles_raw.get("chains") or {})
    problems = []
    for entry in entries:
        for chain in entry.chains:
            if chain == DISPATCHED or chain in known:
                continue
            listed = ", ".join(sorted(known)) or "(none defined)"
            problems.append(
                f"entry {entry.name!r} names chain {chain!r}, which roles.yaml does "
                f"not define — known chains: {listed}"
            )
    return problems


def check_row_costs(
    schedule: dict[str, Any], roles_raw: dict[str, Any], entries: tuple[Entry, ...]
) -> list[str]:
    """A row's declared cost matches the roles its own config names.

    Catches the drift that matters: an entry that reads free on the floor plan
    while its config points a stage at a billed chain.
    """
    by_name = {e.name: e for e in entries}
    roles = roles_raw.get("roles") or {}
    problems = []
    for row in schedule.get("jobs") or []:
        if not isinstance(row, dict):
            continue
        entry = by_name.get(str(row.get("name") or ""))
        if entry is None:
            continue  # check_rows has already said so
        named = _row_roles(row)
        if not named:
            continue
        if entry.kind is Kind.DETERMINISTIC:
            problems.append(
                f"entry {entry.name!r} is deterministic and its schedule config names "
                f"role(s) {sorted(named)} — one of the two is wrong"
            )
            continue
        named_chains = [c for c in entry.chains if c != DISPATCHED]
        if not named_chains:
            # Every chain this entry rides belongs to the work it dispatches,
            # so there is nothing to compare a role against. An entry that
            # names DISPATCHED *alongside* real chains is not that case — it
            # has one stage whose chain is the work's and others whose is not,
            # and skipping the whole row for the first would stop checking the
            # rest. That is how a row acquires a free pass by growing.
            continue
        for role_name in sorted(named):
            block = roles.get(role_name)
            if not isinstance(block, dict):
                problems.append(
                    f"row {entry.name!r} names role {role_name!r}, which roles.yaml "
                    "does not define"
                )
                continue
            chain = block.get("chain")
            if not isinstance(chain, str):
                continue  # the role pins its own refs; there is no chain to compare
            if chain not in named_chains:
                problems.append(
                    f"entry {entry.name!r} names chain(s) {list(entry.chains)} and its "
                    f"role {role_name!r} rides {chain!r} — the floor plan would show "
                    "the wrong model paying for this"
                )
    return problems


def check_substrates(boot_raw: dict[str, Any], entries: tuple[Entry, ...]) -> list[str]:
    """A named substrate is one the boot graph carries.

    The service half of the manifest is otherwise held to its `site`, and a
    site is a loop the source scan can find. Four services wait on IO instead —
    a long poll, a subscription, a filesystem watch — and no clock-scan reaches
    them. What reaches them is this: they are started by a boot substrate, so a
    substrate that is renamed or dropped takes its entry with it rather than
    leaving one that describes nothing.
    """
    carried: set[str] = set()
    for layer in boot_raw.get("layers") or []:
        if isinstance(layer, dict):
            carried.update(str(name) for name in (layer.get("carries") or []))
    problems = []
    for entry in entries:
        if entry.substrate and entry.substrate not in carried:
            listed = ", ".join(sorted(carried)) or "(none)"
            problems.append(
                f"entry {entry.name!r} says substrate {entry.substrate!r} starts it, "
                f"and boot.yaml carries no such substrate — known: {listed}"
            )
    return problems


def verify(schedule_path: Path, roles_path: Path, boot_path: Path) -> None:
    """Raise unless the manifest describes what is shipping. Idempotent."""
    schedule = _read(schedule_path)
    roles_raw = _read(roles_path)
    boot_raw = _read(boot_path)
    problems = (
        check_rows(schedule)
        + check_chains(roles_raw, ENTRIES)
        + check_row_costs(schedule, roles_raw, ENTRIES)
        + check_substrates(boot_raw, ENTRIES)
    )
    if problems:
        raise ManifestError(
            "the manifest does not describe what is about to run:\n  - "
            + "\n  - ".join(problems)
        )


def verify_live() -> None:
    """`verify` against the trees this install actually ships."""
    from tesseract import paths
    from tesseract.scheduler.config_loader import system_schedule_path

    system = paths.system_config_dir()
    verify(system_schedule_path(), system / "roles.yaml", system / "boot.yaml")


__all__ = [
    "check_chains",
    "check_row_costs",
    "check_rows",
    "check_substrates",
    "verify",
    "verify_live",
]
