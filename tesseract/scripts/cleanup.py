"""Manual janitor verb.

    python -m tesseract.scripts.cleanup            # apply
    python -m tesseract.scripts.cleanup --dry-run  # report only

Same sweep the supervisor boot and the `janitor_sweep` scheduled job
run (Docs/Plan/janitor/PLAN.md)."""

from __future__ import annotations

import argparse
import logging
import sys

from tesseract.janitor import run_sweep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Janitor sweep")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be cleaned without touching anything",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    report = run_sweep(dry_run=args.dry_run)
    for finding in report.findings:
        line = f"[{finding.sweep}] {finding.action}: {finding.target}"
        if finding.detail:
            line += f" — {finding.detail}"
        print(line)
    for error in report.errors:
        print(f"[error] {error}", file=sys.stderr)
    print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
