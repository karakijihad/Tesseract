"""Relocate an existing install to the three-sibling layout.

Before: every state directory sits at the install root, with `app/` and
`runtime/` among them. After: `app/`, `home/` and `runtime/` are siblings and
everything else lives inside one of them.

Classification is data, not logic. An entry this script does not recognise is
reported, never guessed — silently picking a destination for an unknown
directory is how data gets lost.

    python -m tesseract.scripts.migrate_install_layout --dry-run
    python -m tesseract.scripts.migrate_install_layout --apply

`--revert` is the inverse, derived from the same classification tuples so
the two directions cannot drift. It moves back only what the forward pass
would have placed, leaves anything else where it is, and refuses rather
than merging onto a destination that already exists:

    python -m tesseract.scripts.migrate_install_layout --dry-run --revert
    python -m tesseract.scripts.migrate_install_layout --apply --revert
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

USER_DIRS = (
    "agenda", "agents", "autonomy", "config", "downloads", "uploads",
    "memory-store", "vault", "workspace", "workshop", "sessions",
    "operator_journal", "telegram", "workers", "missions",
)
USER_FILES = (".env",)

# `controller/` holds named-lane bindings keyed by lane id, which are
# per-machine. Absent on this install, so the first dry-run classified
# cleanly by luck of absence — named here so another machine does not hit
# a refusal.
MACHINE_DIRS = (
    "venv", "EBWebView", "browser", "run", "agent_controller", "scheduler",
    "controller",
)
MACHINE_FILES = (
    "session_metadata.sqlite", "work_index.sqlite",
    "work_index.sqlite-wal", "work_index.sqlite-shm",
    # Trusted working directories are absolute paths on THIS machine.
    "trusted_dirs.json",
)

# The data-sync repo tracks the operator's world, which becomes `home/`.
# Its metadata travels with what it tracks or the next pull rewrites the
# wrong tree.
DATA_REPO_ENTRIES = (".git", ".gitignore")

# Siblings-to-be, and the tree that splits. Never "moved".
_ROOTS = ("app", "home", "runtime")
_LOGS = "logs"

# DESIGN.md §logs — split by ownership.
LOGS_TO_HOME = (
    "sessions", "observer", "conscience", "autonomy", "consolidator",
    "feedback-sweep", "skills", "schedule", "channels",
    # Not in the design's table, found on the live install. The
    # WorkspaceEvent stream is the operator's inbox — items awaiting their
    # decision — so it follows them to the other PC like the rest of this
    # column, rather than staying with machine ops.
    "workspace",
)
LOGS_TO_RUNTIME = (
    "audit", "approvals.jsonl", "circuit-breakers", "supervisor", "janitor",
    "provider-health", "tokenjuice", "governor", "capability-snapshot.json",
    # Console and daemon logs: machine ops, named here because they are files
    # at the top of logs/ rather than subdirectories.
    "backend-console.log", "mirror-backend.log", "shell.log",
    "supervisor-console.log", "supervisor.log",
    "agent-controller-console.log", "agent-controller.log",
    "cost-tracking.jsonl",
)

# Template trees seeded into home/, used to pre-populate the phase-5 manifest.
_TEMPLATE_TREES = ("config", "workspace", "memory-store", "vault", "workshop")


@dataclass(frozen=True)
class MigrationPlan:
    root: Path
    moves: tuple[tuple[Path, Path], ...]
    unclassified: tuple[Path, ...]


def _classify_log_entry(entry: Path, root: Path) -> tuple[Path, Path] | None:
    if entry.name in LOGS_TO_HOME:
        return (entry, root / "home" / _LOGS / entry.name)
    if entry.name in LOGS_TO_RUNTIME:
        return (entry, root / "runtime" / _LOGS / entry.name)
    return None


def plan_migration(root: Path) -> MigrationPlan:
    """Decide where every root entry belongs. Touches nothing."""
    moves: list[tuple[Path, Path]] = []
    unclassified: list[Path] = []

    for entry in sorted(root.iterdir()):
        name = entry.name

        if name in _ROOTS:
            continue  # already a sibling, or the destination itself

        if name == _LOGS:
            for log_entry in sorted(entry.iterdir()):
                classified = _classify_log_entry(log_entry, root)
                if classified is None:
                    unclassified.append(log_entry)
                else:
                    moves.append(classified)
            continue

        if name in USER_DIRS or name in USER_FILES or name in DATA_REPO_ENTRIES:
            moves.append((entry, root / "home" / name))
        elif name in MACHINE_DIRS or name in MACHINE_FILES:
            moves.append((entry, root / "runtime" / name))
        else:
            unclassified.append(entry)

    return MigrationPlan(root=root, moves=tuple(moves), unclassified=tuple(unclassified))


def _supervisor_is_live(root: Path) -> str | None:
    """Mirrors the operator's sync.ps1 guard: a pidfile alone is not proof —
    it checks whether that pid is actually alive, and separately whether
    TESSERACT.exe is running. A stale pidfile must not block a migration."""
    from tesseract.supervisor.process_probe import pid_alive

    pid_file = root / "runtime" / "supervisor.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and pid_alive(pid):
            return f"TESSERACT is running (supervisor pid {pid}). Quit it first."

    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == "tesseract.exe":
                return "TESSERACT.exe is running. Quit it first."
    except Exception:  # noqa: BLE001 - probe must never be the reason a migration fails
        pass
    return None


def _template_paths_present(root: Path) -> list[str]:
    """Home-relative paths of template files the install already has.

    Recorded so phase 5's additive seeding treats this install as already
    decided rather than re-copying into it.
    """
    app_templates = root / "app" / "tesseract"
    home = root / "home"
    present: list[str] = []
    for tree in _TEMPLATE_TREES:
        source = app_templates / tree
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if any(part in ("__pycache__", "_shipping") for part in relative.parts):
                continue
            if not path.is_file() or path.suffix in (".py", ".pyc", ".pyo"):
                continue
            if (home / tree / relative).exists():
                present.append((Path(tree) / relative).as_posix())
    return sorted(present)


def apply_migration(plan: MigrationPlan) -> None:
    """Perform the moves. Refuses rather than guessing or racing."""
    if plan.unclassified:
        listed = "\n  ".join(str(p) for p in plan.unclassified)
        raise RuntimeError(
            "migration refused: these entries are not classified as user, "
            f"machine or app data:\n  {listed}\n"
            "Add them to the tuples in this module — do not let the migration "
            "guess where they belong."
        )

    running = _supervisor_is_live(plan.root)
    if running is not None:
        raise RuntimeError(f"migration refused: {running}")

    for destination_root in ("home", "runtime"):
        (plan.root / destination_root).mkdir(parents=True, exist_ok=True)

    for source, destination in plan.moves:
        if not source.exists():
            continue  # already moved by an earlier, interrupted run
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue  # idempotent: a prior run placed it
        shutil.move(str(source), str(destination))

    # The split empties logs/ but leaves the directory. Remove it only if it
    # really is empty — anything still inside was never classified, and
    # deleting that would be exactly the silent data loss this refuses to do.
    old_logs = plan.root / _LOGS
    if old_logs.is_dir() and not any(old_logs.iterdir()):
        old_logs.rmdir()

    _write_seeded_manifest(plan.root)


def plan_revert(root: Path) -> MigrationPlan:
    """The inverse of `plan_migration`, derived from the same tuples.

    Deliberately NOT read from a journal written by the forward pass. A
    journal would only cover runs made after it shipped, and the install
    that most needs an inverse is the one already migrated — the dev PC,
    moved before any of this existed. Deriving both directions from one set
    of tuples also means they cannot drift apart.

    Only entries the forward pass would have placed are moved back. State
    the app created after the migration is left where it is and reported,
    because "put this at the install root" is a guess for anything the
    classification lists do not name — the same refusal the forward pass
    makes.
    """
    moves: list[tuple[Path, Path]] = []
    unrecognised: list[Path] = []

    known: dict[str, tuple[str, ...]] = {
        "home": USER_DIRS + USER_FILES + DATA_REPO_ENTRIES,
        "runtime": MACHINE_DIRS + MACHINE_FILES,
    }
    for holder, names in known.items():
        base = root / holder
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name == _LOGS:
                continue  # reassembled below
            if entry.name in names:
                moves.append((entry, root / entry.name))
            elif not (holder == "runtime" and entry.name == "seeded.json"):
                unrecognised.append(entry)

    # `logs/` split by ownership on the way in; the two lists are disjoint by
    # name, so merging them back into one directory cannot collide.
    for holder, names in (("home", LOGS_TO_HOME), ("runtime", LOGS_TO_RUNTIME)):
        base = root / holder / _LOGS
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.name in names:
                moves.append((entry, root / _LOGS / entry.name))
            else:
                unrecognised.append(entry)

    return MigrationPlan(root=root, moves=tuple(moves), unclassified=tuple(unrecognised))


def apply_revert(plan: MigrationPlan) -> None:
    """Move everything back. Same refusals as the forward pass."""
    running = _supervisor_is_live(plan.root)
    if running is not None:
        raise RuntimeError(f"revert refused: {running}")

    collisions = [
        destination
        for source, destination in plan.moves
        if destination.exists() and source.exists()
    ]
    if collisions:
        listed = "\n  ".join(str(p) for p in collisions)
        raise RuntimeError(
            "revert refused: these destinations already exist at the install "
            f"root:\n  {listed}\n"
            "Moving onto them would merge two trees silently. Resolve by hand."
        )

    for source, destination in plan.moves:
        if not source.exists():
            continue  # already reverted by an earlier, interrupted run
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))

    # The manifest is an artefact of the forward pass, not operator state, so
    # it goes with it — but only if it is that artefact, identified by the
    # `home` key the migration writes. A manifest of any other shape is left
    # alone rather than deleted on a guess.
    manifest = plan.root / "runtime" / "seeded.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("home") == str(plan.root / "home"):
            manifest.unlink()

    # Remove the directories the split created, and only if they really are
    # empty. Anything left inside was never classified, and deleting that is
    # the silent data loss this refuses to do.
    for leftover in (
        plan.root / "home" / _LOGS,
        plan.root / "runtime" / _LOGS,
        plan.root / "home",
    ):
        if leftover.is_dir() and not any(leftover.iterdir()):
            leftover.rmdir()


def _write_seeded_manifest(root: Path) -> None:
    manifest = root / "runtime" / "seeded.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {"home": str(root / "home"), "paths": _template_paths_present(root)}
    staging = manifest.with_name(f"{manifest.name}.tmp")
    staging.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    staging.replace(manifest)


def _default_root() -> Path:
    from tesseract.paths import home_dir

    return home_dir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tesseract.scripts.migrate_install_layout")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    mode.add_argument("--apply", action="store_true", help="perform the move")
    # A direction, not a mode — so `--dry-run --revert` previews the inverse.
    # An inverse you cannot preview is not much use as a recovery path.
    parser.add_argument(
        "--revert",
        action="store_true",
        help="move the three-sibling layout back to a flat install root",
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="install root to migrate or revert"
    )
    args = parser.parse_args(argv)

    root = args.root or _default_root()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    reverting = args.revert
    plan = plan_revert(root) if reverting else plan_migration(root)
    verb = "revert" if reverting else "migration"

    print(f"install root: {root}")
    print(f"{len(plan.moves)} move(s):")
    for source, destination in plan.moves:
        print(f"  {source.relative_to(root)}  ->  {destination.relative_to(root)}")

    if plan.unclassified:
        if reverting:
            # Left in place, not fatal: these arrived after the migration, and
            # the root is not where they came from.
            print(f"\n{len(plan.unclassified)} left in place (not placed by the migration):")
        else:
            print(f"\n{len(plan.unclassified)} UNCLASSIFIED — migration will refuse:")
        for path in plan.unclassified:
            print(f"  {path.relative_to(root)}")

    if args.dry_run:
        return 1 if plan.unclassified else 0

    if reverting:
        apply_revert(plan)
    else:
        apply_migration(plan)
    print(f"\n{verb} applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
