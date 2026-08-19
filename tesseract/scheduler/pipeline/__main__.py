"""`python -m tesseract.scheduler.pipeline` — the pipeline without the schedule.

    --list            every row and its stages, in the order they run
    --row <name>      run one row now
    --stage <name>    run one stage now, on its own
    --check           the config boot checks, exit 1 if any finding

A stage stays individually runnable on purpose: the thing cron did well was
letting an operator fire one job and watch it, and a pipeline that could only
be run whole would take that away.

**Most stages need the running backend.** `memory_lint` reads
`app["memory_bundle"]`, `vault_lint` reads `app["tool_registry"]`, and there is
no app in a bare CLI process — so run those through the Mirror
(`/schedule-run-now consolidate`) and use this for the deterministic ones,
for `--list` and for `--check`. Rather than let that be discovered as a
confusing failure, `--stage` says which it is before it runs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from tesseract.paths import config_dir
from tesseract.scheduler.pipeline.checks import run_config_checks
from tesseract.scheduler.pipeline.registry import find_stage, row, rows
from tesseract.scheduler.pipeline.runner import PipelineRunner
from tesseract.scheduler.pipeline.stages import CAPTURE_ROW  # noqa: F401 — registers the rows


# Stages whose job returns "<key> unavailable" without the running backend —
# verified against each module's own early-return, not assumed. Running one of
# these from a bare CLI process cannot work, so the CLI says which and where to
# run it instead of producing a failed manifest row.
NEEDS_APP: dict[str, str] = {
    "memory_lint": "memory_bundle",
    "memory_scrub": "memory_bundle",
    "index_rebuild": "memory_bundle",
    "librarian_heartbeat": "memory_bundle",
    "dream_cycle": "memory_bundle",
    "vault_lint": "tool_registry",
}


def _runner(target) -> PipelineRunner:
    return PipelineRunner(target.stages, external_reads=target.external_reads)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tesseract.scheduler.pipeline")
    parser.add_argument("--list", action="store_true", help="rows and their stages")
    parser.add_argument("--row", help="run one row by name")
    parser.add_argument("--stage", help="run one stage by name")
    parser.add_argument("--check", action="store_true", help="run the config boot checks")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.check:
        findings = run_config_checks(config_dir())
        for finding in findings:
            print(finding)
        print(f"{len(findings)} finding(s)")
        return 1 if findings else 0

    if args.list:
        for target in rows():
            print(f"[{target.name}] imports={','.join(target.imports) or '-'}")
            for stage in _runner(target).stages:
                marks = []
                if stage.per_day:
                    marks.append("walks-missed-days")
                if stage.retries:
                    marks.append(f"retries={stage.retries}")
                if stage.name in NEEDS_APP:
                    marks.append(f"needs-{NEEDS_APP[stage.name]}")
                print(
                    f"  {stage.name}\t{stage.cadence.value}\t{stage.kind.value}\t"
                    f"reads={','.join(stage.reads) or '-'}\t"
                    f"writes={','.join(stage.writes) or '-'}\t"
                    f"after={','.join(stage.after) or '-'}"
                    + (f"\t{' '.join(marks)}" if marks else "")
                )
        return 0

    if args.stage:
        found = find_stage(args.stage)
        if found is None:
            print(f"no stage named {args.stage!r}")
            return 1
        owner, _ = found
        if args.stage in NEEDS_APP:
            print(
                f"{args.stage} reads the running backend "
                f"({NEEDS_APP[args.stage]}), which a bare CLI process has no "
                "handle on. Run it through the Mirror instead:\n"
                f"  /schedule-run-now {owner.name}"
            )
            return 2
        result = asyncio.run(_runner(owner).run_one(args.stage))
        print(f"{result.stage}: {result.outcome.value} {result.reason}".rstrip())
        return 0

    if args.row:
        target = row(args.row)
        if target is None:
            print(f"no row named {args.row!r}")
            return 1
        manifest = asyncio.run(_runner(target).run())
        for line in manifest.rows:
            print(f"{line.stage}: {line.outcome.value} {line.reason}".rstrip())
        if manifest.not_due:
            print(f"not due: {', '.join(manifest.not_due)}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
