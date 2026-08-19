"""One-shot rebuild of the two derived indexes.

Walks every chat record (``sessions/chats/*.json``) and every workshop
document (``workshop/**/*.md``), indexing their chunks into
``<TESSERACT_HOME>/work_index.sqlite``, then rebuilds
``<TESSERACT_HOME>/chat_metadata.sqlite`` from the same records.
Idempotent — re-running yields the same row counts.

Both stores are derived: the JSON records are canonical and untouched here,
so dropping either sqlite file and re-running this is the recovery path.

Usage::

    python -m tesseract.scripts.work_index_backfill
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from tesseract.memory.work_index import WorkIndex
from tesseract.memory.work_ingester import backfill
from tesseract.mirror.server import chat_store


def _default_home() -> Path:
    home = os.environ.get("TESSERACT_HOME")
    if home:
        return Path(home)
    # Fallback: repo root.
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the work-history and chat-metadata indexes.")
    parser.add_argument(
        "--workshop", type=Path, default=None,
        help="Override workshop dir (default: <TESSERACT_HOME>/workshop).",
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
    workshop = args.workshop or (home / "workshop")
    db_path = args.db or (home / "work_index.sqlite")

    print("work_index_backfill:")
    print(f"  chats    : {chat_store.chats_dir()}")
    print(f"  workshop : {workshop}")
    print(f"  db       : {db_path}")

    idx = WorkIndex(db_path)
    counts = backfill(
        idx,
        chat_files=chat_store.iter_history_files(),
        workshop_dir=workshop,
    )
    print(f"  indexed  : {counts['chats']} chat chunks, "
          f"{counts['workshop']} workshop chunks")
    print(f"  work     : {idx.count()} rows")

    # The chat-metadata index backs the drawer's day view; without a rebuild
    # the first render falls back to parsing every transcript on disk.
    print(f"  meta db  : {chat_store.metadata_index_path()}")
    print(f"  metadata : {chat_store.rebuild_metadata_index()} chat rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
