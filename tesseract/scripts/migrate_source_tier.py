"""Stamp the provenance tier onto records written before there was one.

OT-1 resolves a record's tier at parse time and writes it on the record's next
write, so the store converges on its own and nothing had to be rewritten. That
is right for a shipped install, where a user's store migrates as it is used. It
is not enough here: a record nothing touches again keeps no tier on disk, so
the file stops being the whole truth, and `derived_from` can still be recovered
today from the `source_path` the two derived writers already recorded, while
somebody remembers that is where it lives.

What it does, per record:

* Resolves the tier from `source_type` through the one declared table and
  writes it into the frontmatter.
* For a `derived` record with no `derived_from`, adopts `source_path` as its
  locator and sets `derivation_depth` to 1. The locator points outside the
  memory store (a daily note, a brief, a plan file), which is what makes each
  of these a FIRST summary rather than a link in a chain.
* Leaves the body untouched. Only frontmatter is rewritten, so no embedding
  and no index entry goes stale.

**An unrecognised `source_type` is reported, never quietly resolved.** The
declared table is built from the writers alive in the code, and a store outlives
its writers: `autonomy_heartbeat` is in this one 13 times from a job that no
longer exists, and the safe-direction fallback would have demoted 13 first-hand
observations to `derived`. Run with no flags, read the unrecognised list, decide
what each one is, add it to the table, and only then write.

    python -m tesseract.scripts.migrate_source_tier            # report only
    python -m tesseract.scripts.migrate_source_tier --write    # rewrite
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.memory.store import RECORD_SUBDIRS, MemoryStore
from tesseract.memory.types import (
    _TIER_BY_SOURCE_TYPE,
    MemoryFrontmatter,
    SourceTier,
    tier_for,
)


def _record_files(store_dir: Path) -> list[Path]:
    out: list[Path] = []
    for subdir in RECORD_SUBDIRS:
        base = store_dir / subdir
        if base.exists():
            out.extend(sorted(base.rglob("*.md")))
    return out


def _is_declared(source_type: str) -> bool:
    """Whether the table names this value, rather than the fallback catching it.

    A value the fallback caught is not wrong by itself, but it is a value
    nobody has looked at, and the difference between those two is the entire
    reason this script reports before it writes.
    """
    value = (source_type or "").strip()
    if value in _TIER_BY_SOURCE_TYPE:
        return True
    return tier_for(value) is not SourceTier.DERIVED or value in _TIER_BY_SOURCE_TYPE


def _split(text: str) -> tuple[dict, str] | None:
    """Frontmatter dict and the body after it, or None when there is none."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data, parts[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--store",
        type=Path,
        default=Path("tesseract/memory-store"),
        help="memory store root (default: tesseract/memory-store)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the records. Without it, nothing is written.",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="copy the store here before writing. Defaults to a sibling "
        "'<store>.pre-tier-<stamp>'; pass a path to choose one.",
    )
    args = parser.parse_args(argv)

    store_dir: Path = args.store
    if not store_dir.exists():
        print(f"no store at {store_dir}", file=sys.stderr)
        return 2

    files = _record_files(store_dir)
    tiers: Counter[str] = Counter()
    unrecognised: Counter[str] = Counter()
    would_stamp_tier = 0
    would_recover: list[tuple[Path, str]] = []
    derived_without_locator: list[Path] = []
    unparsed: list[Path] = []

    planned: list[tuple[Path, dict, str]] = []

    for path in files:
        text = path.read_text(encoding="utf-8")
        split = _split(text)
        if split is None:
            continue
        data, body = split
        try:
            fm = MemoryFrontmatter.from_yaml_dict(data)
        except Exception:  # noqa: BLE001
            unparsed.append(path)
            continue

        source_type = (fm.source_type or "").strip()
        tier = tier_for(source_type)
        tiers[tier.value] += 1
        if not _is_declared(source_type):
            unrecognised[source_type or "<unset>"] += 1

        changed = False
        if data.get("source_tier") != tier.value:
            data["source_tier"] = tier.value
            would_stamp_tier += 1
            changed = True

        if tier is SourceTier.DERIVED and not data.get("derived_from"):
            if fm.source_path:
                # The locator these writers already recorded. It points
                # outside the memory store, so the chain ends there at depth
                # 0 and this record is depth 1.
                data["derived_from"] = [fm.source_path]
                data["derivation_depth"] = 1
                would_recover.append((path, fm.source_path))
                changed = True
            else:
                derived_without_locator.append(path)

        if changed:
            planned.append((path, data, body))

    print(f"store: {store_dir}")
    print(f"records read: {sum(tiers.values())}")
    print(f"tiers: {dict(sorted(tiers.items()))}")
    print(f"records needing a tier stamped: {would_stamp_tier}")
    print(f"derived records whose locator is recoverable: {len(would_recover)}")
    print(
        "derived records with no locator to recover (left as they are): "
        f"{len(derived_without_locator)}"
    )
    if unparsed:
        print(f"records that would not parse (skipped): {len(unparsed)}")
        for path in unparsed:
            print(f"    {path}")

    if unrecognised:
        print(
            "\nUNRECOGNISED source_type values. The fallback resolves each to "
            "`derived`.\nDecide what each one is and add it to "
            "`types.py::_TIER_BY_SOURCE_TYPE` before writing:"
        )
        for value, count in unrecognised.most_common():
            print(f"    {count:4d}  {value!r}")

    if not args.write:
        print(f"\nreport only. {len(planned)} records would change.")
        print("re-run with --write to apply.")
        return 0

    if unrecognised:
        print(
            "\nrefusing to write while a source_type is unrecognised: every "
            "one of those records would be stamped `derived` on a guess.",
            file=sys.stderr,
        )
        return 1

    backup = args.backup or store_dir.with_name(
        f"{store_dir.name}.pre-tier-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    if backup.exists():
        print(f"backup path already exists: {backup}", file=sys.stderr)
        return 1
    shutil.copytree(store_dir, backup)
    print(f"\nbackup: {backup}")

    for path, data, body in planned:
        content = (
            "---\n"
            + yaml.dump(data, default_flow_style=False, sort_keys=False)
            + "---"
            + body
        )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    print(f"rewrote {len(planned)} records.")

    # Read every record back through the model. A migration that leaves one
    # record unparseable has removed it from retrieval, which is worse than
    # the drift it was fixing.
    store = MemoryStore(store_dir)
    reread = store.list_all()
    print(f"re-read after write: {len(reread)} records parse")
    if len(reread) < sum(tiers.values()) - len(unparsed):
        print(
            "FEWER records parse than before. Restore from the backup above.",
            file=sys.stderr,
        )
        return 1
    missing_tier = [fm.id for fm in reread if fm.source_tier is None]
    if missing_tier:
        print(f"records still without a tier: {missing_tier}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
