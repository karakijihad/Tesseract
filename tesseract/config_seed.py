"""First-run seeding of a relocated ``TESSERACT_HOME/config``.

On a packaged install, ``TESSERACT_HOME`` points outside the source
checkout and its ``config/`` starts empty. Before anything reads config,
``ensure_config_seeded`` copies the packaged default tree
(``TESSERACT_DIR/config``) into the writable ``CONFIG_DIR``.

In dev (``TESSERACT_HOME`` unset), ``CONFIG_DIR`` already equals
``TESSERACT_DIR/config`` — the function returns immediately, never
copying a directory onto itself.

Explicit call only. This module MUST have no import-time file I/O.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def ensure_config_seeded() -> None:
    from tesseract.paths import CONFIG_DIR, TESSERACT_DIR

    default_config = TESSERACT_DIR / "config"
    if CONFIG_DIR.resolve() == default_config.resolve():
        return  # dev: CONFIG_DIR is the source tree itself

    if (CONFIG_DIR / "providers.yaml").exists():
        return  # already seeded — never overwrite operator config

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for src in default_config.iterdir():
        dest = CONFIG_DIR / src.name
        if src.is_dir():
            if src.name == "__pycache__":
                continue
            shutil.copytree(src, dest, dirs_exist_ok=True)
        elif src.suffix == ".yaml":
            shutil.copy2(src, dest)


def _ensure_tree_seeded(src: Path, dest: Path, sentinel: str) -> None:
    if dest.resolve() == src.resolve():
        return  # dev: dest is the source tree itself
    if (dest / sentinel).exists():
        return  # already seeded — never overwrite operator state
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True)


def ensure_workspace_seeded() -> None:
    from tesseract.paths import TESSERACT_DIR, workspace_dir

    _ensure_tree_seeded(TESSERACT_DIR / "workspace", workspace_dir(), "SOUL.md")


def ensure_agents_seeded() -> None:
    from tesseract.paths import TESSERACT_DIR, agents_dir

    src = TESSERACT_DIR / "agents"
    if not src.exists():
        return
    dest = agents_dir()
    if dest.resolve() == src.resolve() or dest.exists():
        return
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyc"))
