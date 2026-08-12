"""Check every dependency against what this version needs, and record it.

Runs in the background on every launch. Reads only: it decides what is true
about this machine and writes one artifact, and never downloads, installs or
starts anything — the fetch scripts and `ensure_ollama` own that, and a probe
that repaired what it was measuring could never report the truth.

Exits 0 whatever it finds. A dependency that is missing or out of date is
information, not a failed command, and this is spawned beside a launch that
must not be blocked by it.

Usage: python -m tesseract.scripts.reconcile_capabilities [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="print the artifact to stdout"
    )
    args = parser.parse_args(argv)

    # Same reason as every other provisioning script: this is spawned into a
    # hidden console, so anything worth knowing has to reach the log.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from tesseract.capability.reconcile import run, summarise

    try:
        state = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 — a failed pass must not fail a launch
        logger.warning("capability: the reconcile pass did not complete (%s)", exc)
        return 0

    if args.json:
        print(json.dumps(json.loads(state.model_dump_json()), indent=2))
    else:
        line = summarise(state)
        for record in state.attention:
            print(f"{record.id}: {record.state.value} — {record.reason or 'no detail'}")
        print(line or "everything is as this version expects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
