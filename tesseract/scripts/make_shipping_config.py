"""Build-time templater: emit the shipped default-config tree (Task 8b/8c).

Config ships from hand-authored templates under `tesseract/config/_shipping/`
— for every `tesseract/config/<name>.yaml` that ships, the output takes
`_shipping/<name>.yaml` verbatim (byte-for-byte). A missing template is a
hard build FAILURE, never a silent fallback to the operator's live file —
that fallback is exactly the leak path this templating approach replaces.
Never touches the source directory; this is a one-way, read-src/write-out
build step.

Every shipped `.yaml` under `tesseract/config/` now has a `_shipping/`
template (Task 8c retired the temporary regex/structural resets that used
to cover providers/roles/permissions/schedule.yaml); there is no allowance
list and no fallback path left in `build_shipping_config`.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def build_shipping_config(src_dir: Path, out_dir: Path) -> None:
    """Write the shipped config tree into `out_dir`.

    For every `src_dir/*.yaml`, copies `src_dir/_shipping/<name>.yaml`
    byte-for-byte. A missing template is a hard build failure — never a
    silent fallback to the operator's live file.
    """
    if src_dir.resolve() == out_dir.resolve():
        raise ValueError(f"build_shipping_config: src_dir and out_dir must differ (both resolve to {src_dir.resolve()})")
    out_dir.mkdir(parents=True, exist_ok=True)
    shipping_dir = src_dir / "_shipping"
    for src_file in src_dir.glob("*.yaml"):
        name = src_file.name
        template = shipping_dir / name
        if not template.is_file():
            raise RuntimeError(
                f"build_shipping_config: missing shipping template for {name} "
                f"(expected {template}) — refusing to fall back to "
                "the operator's live config"
            )
        shutil.copy2(template, out_dir / name)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    build_shipping_config(args.src_dir, args.out_dir)
