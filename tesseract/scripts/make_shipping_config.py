"""Emit the shipped config tree: `tesseract/config/*.yaml`, verbatim.

There used to be a second copy of every file under `config/_shipping/`,
hand-authored, and the build shipped that instead. It existed because the dev
tree held settings a stranger must not receive — `security_mode: headless`,
this machine's `born_at`, the operator's own scheduled jobs and reading list.

**Production is the truth now, and the dev tree carries it.** The values that
differed are gone from here: the config in this repo is the config a user
receives, so there is nothing left to template and no second copy to drift.
Anything genuinely per-machine lives in the installed app's own config, which
first-run setup writes and the operator owns from then on — never here.

What that trades away is worth naming. The old rule was that a file with no
template failed the build rather than falling back to the live one, so nothing
could leak by accident. With one tree that guard has nothing to compare, and
what stops a private value shipping is `audit_release_tree.scan_all`, which
runs over the built output and hard-fails on PII, secrets and work notes.
**So the discipline moved rather than disappeared: a setting that must not
reach a user's machine does not belong in this directory at all.**

Never touches the source directory; this is a one-way, read-src/write-out step.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def build_shipping_config(src_dir: Path, out_dir: Path) -> None:
    """Copy every `src_dir/*.yaml` into `out_dir` byte-for-byte."""
    if src_dir.resolve() == out_dir.resolve():
        raise ValueError(f"build_shipping_config: src_dir and out_dir must differ (both resolve to {src_dir.resolve()})")
    out_dir.mkdir(parents=True, exist_ok=True)
    for src_file in src_dir.glob("*.yaml"):
        shutil.copy2(src_file, out_dir / src_file.name)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    build_shipping_config(args.src_dir, args.out_dir)
