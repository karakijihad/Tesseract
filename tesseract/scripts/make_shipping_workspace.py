"""Build-time templater: emit the shipped default workspace tree (Task 11f).

`tesseract/workspace/` is the operator's private state (SOUL.md, DIARY.md,
USER.md, ...) — entirely gitignored, never copied into the production tree.
A fresh install still needs a starter workspace so `ensure_workspace_seeded`
(`tesseract/config_seed.py`) has something to seed from and the propose/
commit flow (`PROPOSABLE_PATHS` in `tesseract/kernel/workspace_changes.py`)
has files that exist to target. Those neutral starter files live under
`tesseract/workspace/_shipping/*.md` (hand-authored, tracked despite the
parent directory being gitignored — see `.gitignore`) and are copied
byte-for-byte into the output tree's `tesseract/workspace/`.

A missing or empty `_shipping/` dir is a hard build FAILURE, never a silent
skip — that silence is exactly the first-boot crash this module exists to
prevent.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def build_shipping_workspace(shipping_dir: Path, out_dir: Path) -> None:
    """Write the shipped starter workspace into `out_dir`.

    Copies every `shipping_dir/*.md` template verbatim into `out_dir`. Raises
    `RuntimeError` if `shipping_dir` is missing or has no templates — never
    falls back to skipping the workspace or ships from the operator's live
    tree.
    """
    if shipping_dir.resolve() == out_dir.resolve():
        raise ValueError(f"build_shipping_workspace: shipping_dir and out_dir must differ (both resolve to {shipping_dir.resolve()})")
    if not shipping_dir.is_dir():
        raise RuntimeError(
            f"build_shipping_workspace: missing shipping dir ({shipping_dir}) "
            "— refusing to ship without a starter workspace"
        )
    templates = sorted(shipping_dir.glob("*.md"))
    if not templates:
        raise RuntimeError(
            f"build_shipping_workspace: no templates found under {shipping_dir}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    for template in templates:
        shutil.copy2(template, out_dir / template.name)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shipping_dir", type=Path)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    build_shipping_workspace(args.shipping_dir, args.out_dir)
