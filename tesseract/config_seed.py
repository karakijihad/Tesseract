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
    _stamp_born_at_if_empty()


def _stamp_born_at_if_empty() -> None:
    """First-run only (called from the fresh-seed branch above): the
    shipped identity.yaml ships with ``born_at: ""`` so no operator
    timezone leaks into the template. Stamp the instance's actual birth
    time once so the prompt's "Age: day N" line isn't blank forever."""
    import yaml

    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import CONFIG_DIR

    identity_path = CONFIG_DIR / "identity.yaml"
    if not identity_path.exists():
        return
    current = yaml.safe_load(identity_path.read_text(encoding="utf-8")) or {}
    if current.get("born_at"):
        return  # already set — never overwrite

    from datetime import datetime

    now_iso = datetime.now().astimezone().isoformat()
    round_trip_yaml(identity_path, lambda doc: doc.__setitem__("born_at", now_iso))


def _ensure_tree_seeded(src: Path, dest: Path, sentinel: str) -> None:
    if dest.resolve() == src.resolve():
        return  # dev: dest is the source tree itself
    if (dest / sentinel).exists():
        return  # already seeded — never overwrite operator state
    if not src.exists():
        raise RuntimeError(
            f"_ensure_tree_seeded: source tree missing ({src}) — this "
            f"packaged install is broken and cannot seed {dest}. The build "
            "must ship this directory; this should never happen on a "
            "correctly built production tree."
        )
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True)


def ensure_workspace_seeded() -> None:
    from tesseract.paths import TESSERACT_DIR, workspace_dir

    _ensure_tree_seeded(TESSERACT_DIR / "workspace", workspace_dir(), "SOUL.md")


def ensure_memory_store_seeded() -> None:
    """Seed ``<home>/memory-store/`` from the shipped scaffold (``MEMORY.md``,
    ``WHAT_NOT_TO_SAVE.md``, ``.gitignore``) so a fresh install opens on a
    ready-to-use store instead of an empty directory. The per-memory-type
    subdirs (``user/``, ``feedback/``, ...) are not part of this scaffold —
    ``MemoryStore._ensure_dirs()`` creates those lazily on first use."""
    from tesseract.paths import TESSERACT_DIR, home_dir

    _ensure_tree_seeded(TESSERACT_DIR / "memory-store", home_dir() / "memory-store", "MEMORY.md")


def ensure_vault_seeded() -> None:
    """Seed ``<home>/vault/`` from the shipped scaffold (``CATALOG.md``,
    ``.gitignore``) so a fresh install has a ready catalog instead of
    crashing/looking empty before the first ingest."""
    from tesseract.paths import TESSERACT_DIR, home_dir

    _ensure_tree_seeded(TESSERACT_DIR / "vault", home_dir() / "vault", "CATALOG.md")


def ensure_tars_workshop_seeded() -> None:
    """Seed ``<home>/tars-workshop/`` from the shipped scaffold (``INDEX.md``,
    ``README.md``, ``.gitignore``)."""
    from tesseract.paths import TESSERACT_DIR, home_dir

    _ensure_tree_seeded(TESSERACT_DIR / "tars-workshop", home_dir() / "tars-workshop", "INDEX.md")


def ensure_env_seeded() -> None:
    """Copy the tracked ``.env.example`` template to ``<home>/.env`` on a
    fresh relocated ``TESSERACT_HOME``. No-ops in dev (home is the source
    tree) and never overwrites an existing ``.env`` — first-run only."""
    from tesseract.paths import TESSERACT_DIR, home_dir

    home = home_dir()
    if home.resolve() == TESSERACT_DIR.resolve():
        return  # dev: home is the source tree itself

    dest = home / ".env"
    if dest.exists():
        return  # already seeded — never overwrite operator secrets

    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TESSERACT_DIR / ".env.example", dest)


def ensure_agents_seeded() -> None:
    from tesseract.paths import TESSERACT_DIR, agents_dir

    src = TESSERACT_DIR / "agents"
    if not src.exists():
        return
    dest = agents_dir()
    if dest.resolve() == src.resolve() or dest.exists():
        return
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyc"))
