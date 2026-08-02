"""Seeding of a relocated ``TESSERACT_HOME`` from the shipped templates.

On a packaged install the state trees start empty and the factory copies
live in the sealed app tree. Seeding is **additive**: every boot copies in
template files the install does not have, and records what it copied in
``runtime/seeded.json``.

That manifest is what lets "missing" and "deliberately deleted" be told
apart. Without it the only safe rule is "seed once, never again", which is
what the old sentinel gate did — and it meant a default added in a later
release never reached an existing install. The operator's copy always wins
over the template, and a file they delete stays deleted.

The manifest lives under ``runtime/`` because it describes what *this*
machine has done; it must not travel with ``home/`` to another PC.

In dev (``TESSERACT_HOME`` unset) every destination IS its own template
source, so each function returns before copying a tree onto itself.

Explicit call only. This module MUST have no import-time file I/O.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path

# Templates are data. Python source under a template tree is packaging
# residue (``config/`` holds loader modules beside its yaml) and must never
# be copied into the operator's writable tree, where it would shadow the
# real module and survive updates that replace the app.
_SKIP_DIR_NAMES = frozenset({"__pycache__", "_shipping"})
_SKIP_SUFFIXES = frozenset({".py", ".pyc", ".pyo"})

# The shipped state trees carry a `.gitignore` of `*` with only their scaffold
# files negated (`build_production_tree._write_state_dir_gitignore`), so that
# operator content cannot be committed by accident if an install happens to sit
# inside a git repo. Copying it into `home/` defeats the operator's data-sync
# repo, which is a git repo there ON PURPOSE — it silently kept the entire
# memory store, vault and workshop out of their backups. What `home/` ignores
# is the sync repo's business, not the app's.
_SKIP_NAMES = frozenset({".gitignore"})


def _manifest_path() -> Path:
    from tesseract.paths import runtime_dir

    return runtime_dir() / "seeded.json"


def load_seeded() -> set[str]:
    """Home-relative POSIX paths this install has already seeded.

    A missing or malformed manifest degrades to "seed whatever is missing"
    rather than crashing the boot — the worst outcome of a truncated write
    is one re-seeded file, and that is far better than an install that
    won't start.

    The manifest records which home it describes and is ignored when that
    does not match. It sits under ``runtime/``, a sibling of ``home/``, so
    the two are only paired by convention: anything else sharing that
    install root would otherwise read a manifest written for a different
    home and skip seeding files that home never received.
    """
    from tesseract.paths import home_dir

    try:
        raw = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, dict) or raw.get("home") != str(home_dir()):
        return set()
    paths = raw.get("paths")
    return {str(entry) for entry in paths} if isinstance(paths, list) else set()


def record_seeded(paths: Iterable[str]) -> None:
    """Merge `paths` into the manifest. Written temp-then-rename so a power
    loss mid-write leaves the previous manifest intact rather than a
    half-written one."""
    from tesseract.paths import home_dir

    added = set(paths)
    if not added:
        return
    target = _manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"home": str(home_dir()), "paths": sorted(load_seeded() | added)}, indent=2
    )
    staging = target.with_name(f"{target.name}.tmp")
    staging.write_text(payload, encoding="utf-8")
    staging.replace(target)


def seed_tree(src: Path, dest: Path) -> list[str]:
    """Copy template files missing from `dest`; return their relative paths.

    Skips anything already present (the operator's copy wins) and anything
    the manifest lists (they deleted it on purpose). Symlinks are skipped
    outright — following one would copy a file from outside the template
    tree into the operator's tree.

    Returned paths are relative to ``home``, not to `src`. One manifest
    covers every tree, so a key must say which tree it belongs to: bare
    ``.gitignore`` from ``workspace/`` would otherwise suppress ``vault/``'s
    own ``.gitignore`` on the next boot.
    """
    from tesseract.paths import home_dir

    home = home_dir()
    already = load_seeded()
    added: list[str] = []

    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        if any(part in _SKIP_DIR_NAMES for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix in _SKIP_SUFFIXES or path.name in _SKIP_NAMES:
            continue

        target = dest / relative
        try:
            key = target.relative_to(home).as_posix()
        except ValueError:
            key = relative.as_posix()  # dest outside home: no tree to qualify by
        if target.exists() or key in already:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        added.append(key)

    return added


def _seed_from_templates(template_name: str, dest: Path) -> None:
    from tesseract.paths import TESSERACT_DIR

    src = TESSERACT_DIR / template_name
    if dest.resolve() == src.resolve():
        return  # dev: dest is the source tree itself
    if not src.exists():
        raise RuntimeError(
            f"seed: source tree missing ({src}) — this packaged install is "
            f"broken and cannot seed {dest}. The build must ship this "
            "directory; this should never happen on a correctly built "
            "production tree."
        )
    dest.mkdir(parents=True, exist_ok=True)
    record_seeded(seed_tree(src, dest))


def ensure_config_seeded() -> None:
    from tesseract.paths import config_dir

    _seed_from_templates("config", config_dir())
    _stamp_born_at_if_empty()


def _stamp_born_at_if_empty() -> None:
    """The shipped identity.yaml carries ``born_at: ""`` so no operator
    timezone leaks into the template. Stamp the instance's actual birth time
    once so the prompt's "Age: day N" line isn't blank forever.

    Idempotent, and called on every boot rather than from a one-time seed
    branch — under additive seeding there is no single fresh-seed moment."""
    import yaml

    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import config_dir

    identity_path = config_dir() / "identity.yaml"
    if not identity_path.exists():
        return
    current = yaml.safe_load(identity_path.read_text(encoding="utf-8")) or {}
    if current.get("born_at"):
        return  # already set — never overwrite

    from datetime import datetime

    now_iso = datetime.now().astimezone().isoformat()
    round_trip_yaml(identity_path, lambda doc: doc.__setitem__("born_at", now_iso))


def ensure_workspace_seeded() -> None:
    from tesseract.paths import workspace_dir

    _seed_from_templates("workspace", workspace_dir())


def ensure_memory_store_seeded() -> None:
    """Seed ``<home>/memory-store/`` from the shipped scaffold (``MEMORY.md``,
    ``WHAT_NOT_TO_SAVE.md``, ``.gitignore``) so a fresh install opens on a
    ready-to-use store instead of an empty directory. The per-memory-type
    subdirs (``user/``, ``feedback/``, ...) are not part of this scaffold —
    ``MemoryStore._ensure_dirs()`` creates those lazily on first use."""
    from tesseract.paths import home_dir

    _seed_from_templates("memory-store", home_dir() / "memory-store")


def ensure_vault_seeded() -> None:
    """Seed ``<home>/vault/`` from the shipped scaffold (``CATALOG.md``,
    ``.gitignore``) so a fresh install has a ready catalog instead of
    crashing/looking empty before the first ingest."""
    from tesseract.paths import home_dir

    _seed_from_templates("vault", home_dir() / "vault")


def ensure_tars_workshop_seeded() -> None:
    """Seed ``<home>/tars-workshop/`` from the shipped scaffold (``INDEX.md``,
    ``README.md``, ``.gitignore``)."""
    from tesseract.paths import home_dir

    _seed_from_templates("tars-workshop", home_dir() / "tars-workshop")


def ensure_env_seeded() -> None:
    """Copy the tracked ``.env.example`` template to ``<home>/.env`` on a
    fresh relocated ``TESSERACT_HOME``. No-ops in dev (home is the source
    tree) and never overwrites an existing ``.env`` — first-run only.

    Deliberately not additive: ``.env`` is one file holding secrets, and a
    key the operator removed must never reappear."""
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
