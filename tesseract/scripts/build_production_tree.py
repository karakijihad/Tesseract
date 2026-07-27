"""Generate the sanitized production tree users download.

Reads the dev repo, writes a clean tree: only git-TRACKED files, config
shipped from hand-authored templates via make_shipping_config (Task 8b),
state dirs created empty, a small belt-and-braces glob denylist per
_production_manifest. NEVER writes into the source tree.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path

from ruamel.yaml import YAML

from tesseract.scripts._production_manifest import (
    EMPTY_DIRS,
    EXCLUDE_GLOBS,
    EXCLUDE_PATH_GLOBS,
    EXCLUDE_PATHS,
)
from tesseract.scripts.make_shipping_config import build_shipping_config


def _force_remove(func, path, _exc) -> None:
    """`shutil.rmtree` onexc hook: `copy2` preserves the source read-only bit
    (real files under e.g. `vault/raw/**`), which blocks deletion on Windows.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def tracked_files(src_root: Path) -> list[str]:
    """Repo-root-relative posix paths of every git-TRACKED file.

    Tracking status is the authority on what may ship: anything untracked or
    gitignored is machine-local or operator-private by construction.
    """
    out = subprocess.run(
        ["git", "-C", str(src_root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def _shippable(rel: str) -> bool:
    if any(rel == p or rel.startswith(f"{p}/") for p in EXCLUDE_PATHS):
        return False
    if any(fnmatch.fnmatch(rel, pat) for pat in EXCLUDE_PATH_GLOBS):
        return False
    name = rel.rsplit("/", 1)[-1]
    return not any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def _reset_entities(out_root: Path) -> None:
    """Blank `people` in the shipped `memory/entities.yaml`, if present.

    `people` holds only the operator's own entry; `projects`/`tools`/
    `concepts` are generic TESSERACT taxonomy and ship as-is. Only ever
    touches the OUTPUT copy — this runs after files are already copied.
    """
    entities_path = out_root / "tesseract" / "memory" / "entities.yaml"
    if not entities_path.is_file():
        return
    yaml = YAML()
    data = yaml.load(entities_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "people" in data:
        data["people"] = []
    with entities_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)


def build(src_root: Path, out_root: Path, files: Iterable[str] | None = None) -> None:
    """Copy the shippable tracked files of `src_root` into a fresh `out_root`.

    `files` (repo-root-relative posix paths) defaults to `tracked_files`;
    tests inject an explicit list so they need no git repo.
    """
    src_root, out_root = Path(src_root), Path(out_root)
    if src_root.resolve() == out_root.resolve():
        raise ValueError(f"out_root must differ from src_root ({src_root})")
    if out_root.resolve().is_relative_to(src_root.resolve()):
        raise ValueError("out_root must not live inside src_root")
    if src_root.resolve().is_relative_to(out_root.resolve()):
        # Reverse of the check above: if out_root is an ANCESTOR of src_root,
        # the rmtree below would delete the source tree. Reachable by swapping
        # the two CLI args, so guard it before any destructive write.
        raise ValueError("src_root must not live inside out_root")
    if out_root.exists():
        shutil.rmtree(out_root, onexc=_force_remove)
    out_root.mkdir(parents=True)

    for rel in tracked_files(src_root) if files is None else files:
        if not _shippable(rel):
            continue
        source = src_root / rel
        if source.is_symlink():
            # git tracks the path string, not the bytes: `is_file()`/`copy2`
            # resolve through a symlink, so a tracked link pointing outside the
            # repo would copy its target's LIVE content and defeat the
            # tracked-only allowlist. Skip links entirely (check the link, not
            # its target).
            continue
        if not source.is_file():
            continue
        target = out_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    # Config: overwrite the copied tree with reset versions.
    src_config = src_root / "tesseract" / "config"
    if src_config.is_dir():
        build_shipping_config(src_config, out_root / "tesseract" / "config")

    _reset_entities(out_root)

    for rel in EMPTY_DIRS:
        d = out_root / rel
        if d.exists():
            shutil.rmtree(d, onexc=_force_remove)
        d.mkdir(parents=True)
        (d / ".gitkeep").write_text("", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m tesseract.scripts.build_production_tree")
    ap.add_argument("src_root", type=Path)
    ap.add_argument("out_root", type=Path)
    args = ap.parse_args()
    build(args.src_root, args.out_root)
    print(f"production tree written to {args.out_root}")


if __name__ == "__main__":
    main()
