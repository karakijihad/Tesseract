"""One-shot backfill for the CR-1 work-history index + session-metadata.

Walks ``tesseract/sessions/*.json`` and ``tesseract/tars-workshop/**/*.md``,
indexing every chunk into ``<TESSERACT_HOME>/work_index.sqlite``.
Also rebuilds ``<TESSERACT_HOME>/session_metadata.sqlite`` from the
same session corpus (active + ``archive/YYYY-MM/`` subtree).
Idempotent — re-running yields the same row counts.

Usage::

    python -m tesseract.scripts.work_index_backfill
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from tesseract.memory.session_metadata import SessionMetadataIndex
from tesseract.memory.work_index import WorkIndex
from tesseract.memory.work_ingester import backfill


def _default_home() -> Path:
    home = os.environ.get("TESSERACT_HOME")
    if home:
        return Path(home)
    # Fallback: repo root.
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill the CR-1 work-history index.")
    parser.add_argument(
        "--sessions", type=Path, default=None,
        help="Override sessions dir (default: <TESSERACT_HOME>/sessions).",
    )
    parser.add_argument(
        "--workshop", type=Path, default=None,
        help="Override workshop dir (default: <TESSERACT_HOME>/tars-workshop).",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="Override sqlite path (default: <TESSERACT_HOME>/work_index.sqlite).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    home = _default_home()
    sessions = args.sessions or (home / "sessions")
    workshop = args.workshop or (home / "tars-workshop")
    db_path = args.db or (home / "work_index.sqlite")

    print(f"work_index_backfill:")
    print(f"  sessions : {sessions}")
    print(f"  workshop : {workshop}")
    print(f"  db       : {db_path}")

    idx = WorkIndex(db_path)
    counts = backfill(idx, sessions_dir=sessions, workshop_dir=workshop)
    print(f"  indexed  : {counts['sessions']} session chunks, "
          f"{counts['workshop']} workshop chunks")
    print(f"  work     : {idx.count()} rows")

    # 2026-05-23 — also rebuild the session-metadata derived index. The
    # Mirror drawer's session-list path reads from this; without
    # backfill, the first render falls back to the slow disk walk.
    sm_path = home / "session_metadata.sqlite"
    print(f"  sm db    : {sm_path}")
    sm = SessionMetadataIndex(sm_path)
    sm_count = sm.rebuild_from_disk(sessions)
    print(f"  metadata : {sm_count} session rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
