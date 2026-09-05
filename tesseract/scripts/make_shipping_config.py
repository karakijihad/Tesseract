"""Emit the shipped config tree.

`tesseract/config/*.yaml` is what this machine runs. A file with the same name
under `config/_shipping/` overrides it on the way out, and is what a user
receives. Everything else ships verbatim.

Keep the overlay sparse: only files that genuinely differ belong in it. Every
copy is a second file to keep in step, and `test_shipping_config_templates`
fails when an overlaid file stops matching its live counterpart key for key.

Never touches the source directory; one-way, read-src/write-out.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SHIPPING_DIR_NAME = "_shipping"


def shipping_overrides(src_dir: Path, pattern: str = "*.yaml") -> dict[str, Path]:
    """`{filename: overriding path}` for every file in `src_dir/_shipping`."""
    overlay = src_dir / SHIPPING_DIR_NAME
    if not overlay.is_dir():
        return {}
    return {p.name: p for p in overlay.glob(pattern)}


def build_shipping_overlay(src_dir: Path, out_dir: Path, pattern: str) -> None:
    """Copy `src_dir/<pattern>` into `out_dir`, preferring `_shipping/` copies.

    One implementation, because "a file under `_shipping/` overrides its live
    counterpart" is one idea and had briefly become two: the agents roster was
    folded by an inline `shutil.copy2` that carried none of the guard below, so
    only one of the two overlays refused to ship a file with nothing behind it.
    """
    if src_dir.resolve() == out_dir.resolve():
        raise ValueError(
            f"build_shipping_overlay: src_dir and out_dir must differ "
            f"(both resolve to {src_dir.resolve()})"
        )
    overrides = shipping_overrides(src_dir, pattern)
    out_dir.mkdir(parents=True, exist_ok=True)
    for src_file in src_dir.glob(pattern):
        shutil.copy2(overrides.get(src_file.name, src_file), out_dir / src_file.name)

    unmatched = set(overrides) - {p.name for p in src_dir.glob(pattern)}
    if unmatched:
        raise RuntimeError(
            f"{SHIPPING_DIR_NAME}/ has no live counterpart for: "
            f"{', '.join(sorted(unmatched))}"
        )


def build_shipping_config(src_dir: Path, out_dir: Path) -> None:
    """Copy `src_dir/*.yaml` into `out_dir`, preferring `_shipping/` copies."""
    build_shipping_overlay(src_dir, out_dir, "*.yaml")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    build_shipping_config(args.src_dir, args.out_dir)
