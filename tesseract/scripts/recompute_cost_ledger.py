"""Re-price every historical ledger row against the catalog as it stands now.

Three of six model prices were wrong for months, and cache writes were never
billed at all. Correcting the catalog fixes today onward and leaves the record
disagreeing with itself: the two-week plots would show a step at the correction
date that means nothing about spending. This rewrites the history so the record
is one thing.

**Running it again is safe, and that is a property to preserve.** Every figure
is rebuilt from the token counts on the row rather than adjusted from the figure
already there, so a second pass over its own output produces the same file. The
running totals are recomputed from zero in write order for the same reason. A
maintainer who ever changes this to adjust a value in place breaks that, and the
`test_running_it_twice_changes_nothing` test is what should stop them.

Which means re-running is the remedy after the app writes rows at old prices,
which it does until it restarts and picks up a corrected catalog.

**What it cannot do, it does not guess.** A row naming a model the catalog does
not describe keeps the cost it was written with, and the summary says how many.
Inventing a rate for a model nobody declared is the defect this whole workstream
is about, and doing it here would be doing it to the evidence.

Historical OpenAI rows recompute exactly, which was not obvious: those models
declare `cache_write_from: uncached_input`, so the written volume derives from
`input_tokens - cached_tokens` and both are on every row. Nothing needs the
cache-write counts that were never captured.

    python -m tesseract.scripts.recompute_cost_ledger --dry-run
    python -m tesseract.scripts.recompute_cost_ledger --write
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tesseract.brain.cost.ledger import CostLedger, CostUsage, ModelPrice
from tesseract.config.loader import load_config
from tesseract.paths import home_dir

#: Written beside the ledger after a successful pass: when it ran, what it
#: moved, and where the backup went. Provenance, not a lock. Re-running is safe
#: (see the module docstring) and is sometimes exactly what is wanted.
MARKER_NAME = "cost-tracking.recomputed.json"


def _prices() -> dict[str, ModelPrice]:
    """The catalog as the running app reads it, not a second parse of the
    YAML. A recompute against a different reading of the same file would be a
    third opinion about what a model costs."""
    bundle = load_config()
    ledger = CostLedger.from_bundle(bundle, log_path=Path("nowhere.jsonl"))
    return ledger.pricing


def _reprice(row: dict, pricer: CostLedger) -> float | None:
    """The new cost for one row, or None when the catalog cannot say.

    Priced by the runtime's own `_compute_usd` rather than by arithmetic
    written here. A second implementation of the formula would be a second
    answer to what a token costs, which is the thing being repaired.

    Voice rows carry `kind` rather than `role` and are priced per character or
    per second by a different table; they are left alone by the caller.
    """
    model = row.get("model")
    if not model or model not in pricer.pricing:
        return None
    return pricer._compute_usd(str(row.get("role") or ""), str(model), CostUsage.from_raw(row))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report, change nothing")
    group.add_argument("--write", action="store_true", help="rewrite the ledger")
    args = parser.parse_args()

    ledger_path = home_dir() / "logs" / "cost-tracking.jsonl"
    if not ledger_path.exists():
        print(f"no ledger at {ledger_path}")
        return 1
    marker = ledger_path.parent / MARKER_NAME

    pricer = CostLedger(
        enabled=True,
        warning_at_pct=1.0,
        per_role_caps={},
        pricing=_prices(),
        log_path=Path("nowhere.jsonl"),
    )
    # The running app appends to this file. A rewrite computed from a stale
    # read would silently drop every row written while this ran, so the
    # fingerprint taken here is checked again immediately before the replace.
    before = ledger_path.stat()
    lines = ledger_path.read_text(encoding="utf-8").splitlines()

    rows: list[dict] = []
    unparsed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            unparsed += 1

    # Rebuild the running totals from scratch in the order the rows were
    # written, because a re-priced row changes every total after it.
    daily: dict[str, float] = defaultdict(float)
    per_role: dict[tuple[str, str], float] = defaultdict(float)
    voice_provider: dict[tuple[str, str], float] = defaultdict(float)

    old_total = new_total = 0.0
    unpriced: dict[str, int] = defaultdict(int)
    by_model: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])

    for row in rows:
        day = str(row.get("local_date") or "")
        old = float(row.get("cost_usd") or 0.0)
        old_total += old

        if row.get("kind"):  # voice row, priced by a different table
            new = old
            voice_provider[(day, str(row.get("provider") or ""))] += new
            row["provider_total_usd"] = round(voice_provider[(day, str(row.get("provider") or ""))], 8)
        else:
            recomputed = _reprice(row, pricer)
            if recomputed is None:
                unpriced[str(row.get("model") or "?")] += 1
                new = old
            else:
                new = recomputed
            role = str(row.get("role") or "")
            per_role[(day, role)] += new
            row["role_total_usd"] = round(per_role[(day, role)], 8)
            by_model[str(row.get("model") or "?")][0] += old
            by_model[str(row.get("model") or "?")][1] += new

        new_total += new
        daily[day] += new
        row["cost_usd"] = round(new, 8)
        row["daily_total_usd"] = round(daily[day], 8)

    print(f"ledger        {ledger_path}")
    print(f"rows          {len(rows)} ({unparsed} unparseable, left out)")
    print(f"lifetime      ${old_total:.4f} -> ${new_total:.4f}  ({new_total - old_total:+.4f})")
    print("by model:")
    for model, (old, new) in sorted(by_model.items(), key=lambda kv: -abs(kv[1][1] - kv[1][0])):
        print(f"  {model:<24} ${old:>9.4f} -> ${new:>9.4f}  ({new - old:+.4f})")
    if unpriced:
        print("left at their recorded cost, no catalog entry:")
        for model, count in sorted(unpriced.items()):
            print(f"  {model:<24} {count} rows")

    if args.dry_run:
        print("\ndry run, nothing written")
        return 0

    after = ledger_path.stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        print()
        print("the ledger changed while this was running, so the rewrite would")
        print("drop whatever was appended. Nothing was written.")
        print("Stop the backend, or wait for it to go quiet, and run it again.")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ledger_path.with_suffix(f".jsonl.before-recompute-{stamp}")
    shutil.copy2(ledger_path, backup)

    tmp = ledger_path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    tmp.replace(ledger_path)

    marker.write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "backup": str(backup),
                "rows": len(rows),
                "lifetime_before": round(old_total, 8),
                "lifetime_after": round(new_total, 8),
                "unpriced": dict(unpriced),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nbackup        {backup}")
    print(f"marker        {marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
