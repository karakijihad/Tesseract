"""``python -m tesseract.scripts.clear_crash_storm`` — archive the
crash-storm marker so the supervisor will start again.

The marker (``<TESSERACT_HOME>/runtime/crash_storm.json``) is written
when the supervisor latches after 3 crashes in 5 minutes. While
present, ``python -m tesseract.supervisor`` refuses to start (unless
``--force`` is passed for one-shot bypass). This CLI moves the marker
to ``<TESSERACT_HOME>/logs/supervisor/crash-storm-archive/<timestamp>.json``
so the operator keeps a record across reboots, then clears the live
file.
"""

from __future__ import annotations

import argparse
import sys

from tesseract.paths import TESSERACT_HOME
from tesseract.supervisor.breaker import CrashStormBreaker, crash_storm_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tesseract.scripts.clear_crash_storm",
        description=(
            "Archive the crash-storm marker so the supervisor can start "
            "again. Records the marker under logs/supervisor/"
            "crash-storm-archive/<timestamp>.json so the failure stays "
            "auditable across reboots."
        ),
    )
    parser.parse_args(argv)

    home = TESSERACT_HOME
    breaker = CrashStormBreaker(tesseract_home=home)
    if not breaker.is_latched():
        print(f"clear_crash_storm: no marker at {crash_storm_path(home)} — nothing to clear")
        return 0

    archived = breaker.clear()
    print(f"clear_crash_storm: archived marker → {archived}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
