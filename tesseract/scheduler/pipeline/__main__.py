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
no app in a bare CLI process. Each of those declares the key it cannot run
without, so `--stage` says which it is before it runs rather than letting it be
discovered as a confusing failure, and the same refusal is what the app itself
gives when it is missing one. `run_stage` decides that for every caller; this
module is one of them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from tesseract.paths import config_dir
from tesseract.scheduler.pipeline.checks import run_config_checks
from tesseract.scheduler.pipeline.registry import row, rows
from tesseract.scheduler.pipeline.run_stage import run_stage
from tesseract.scheduler.pipeline.runner import PipelineRunner
from tesseract.scheduler.pipeline.stages import CAPTURE_ROW  # noqa: F401 — registers the rows


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
                for key in stage.needs_app:
                    marks.append(f"needs-{key}")
                print(
                    f"  {stage.name}\t{stage.cadence.value}\t{stage.kind.value}\t"
                    f"reads={','.join(stage.reads) or '-'}\t"
                    f"writes={','.join(stage.writes) or '-'}\t"
                    f"after={','.join(stage.after) or '-'}"
                    + (f"\t{' '.join(marks)}" if marks else "")
                )
        return 0

    if args.stage:
        # No app: this is a bare process, so a stage that declares one is
        # refused here and runs from the app itself. The refusal is written
        # once, in `run_stage`, so every surface says the same thing.
        result = asyncio.run(run_stage(args.stage))
        print(result.line)
        if result.ran:
            return 0
        # 1 for a name nothing declares, 2 for a stage this process cannot
        # run. The old branch drew the same distinction and scripts read it.
        return 1 if not result.found else 2

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
