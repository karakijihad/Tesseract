"""Fetch the reranker model files into `<TESSERACT_HOME>/models/reranker/`.

Reads the pinned source from the providers catalog entry that
`roles.yaml::reranker.primary` points at — no model names, URLs or digests
live here. Idempotent: existing files are kept unless --force. Retrieval
works without these files (pure RRF order); this script is what turns the
reranker on.

Usage: python -m tesseract.scripts.fetch_reranker_model [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys

from tesseract.lib.pinned_fetch import ensure_files, parse_download_block

_LABEL = "reranker"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args(argv)

    # Provisioning and the per-launch retry both spawn this into a hidden
    # console, so progress has to reach the log rather than a terminal
    # nobody sees.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from tesseract.brain.boot import load_reranker_cfg

    cfg = load_reranker_cfg()
    if not cfg:
        print("reranker role not configured (roles.yaml::reranker) — nothing to fetch")
        return 1

    source = parse_download_block(
        cfg.get("download"), where="providers.yaml reranker entry"
    )
    if source is None:
        # `parse_download_block` has already logged which part of the block
        # was missing or malformed.
        return 1

    # Both files land beside each other, and `model_path` / `tokenizer_path`
    # are that directory joined with the catalog's own filenames — which are
    # exactly the keys of the `files:` map.
    dest_dir = cfg["model_path"].parent
    if not ensure_files(source, dest_dir, label=_LABEL, force=args.force):
        print("reranker model incomplete — retrieval keeps pure RRF order")
        return 1

    print("reranker model ready — it loads lazily on the next retrieval")
    return 0


if __name__ == "__main__":
    sys.exit(main())
