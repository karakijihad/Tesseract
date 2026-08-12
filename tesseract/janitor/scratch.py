"""Scratch-dir sweep — the pytest scratch-dir rule, automated.

Deletes matching dirs in the repo root and under `tesseract/`. Roots
resolve at call time (repo root from `tesseract.paths.TESSERACT_DIR`)
so tests can point the sweep elsewhere via the `roots` parameter."""

from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Iterable

from .config import JanitorConfig
from .models import Finding

log = logging.getLogger(__name__)


def _default_roots() -> list[Path]:
    from tesseract.paths import TESSERACT_DIR

    return [TESSERACT_DIR.parent, TESSERACT_DIR]


def _rmtree(path: Path) -> None:
    def _onerror(func, p, _exc):  # read-only files: clear the bit, retry
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_onerror)


def sweep_scratch(
    cfg: JanitorConfig,
    *,
    dry_run: bool,
    roots: Iterable[Path] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[Path] = set()
    for root in roots if roots is not None else _default_roots():
        if not root.is_dir():
            continue
        for glob in cfg.scratch_dir_globs:
            for hit in root.glob(glob):
                if hit in seen or not hit.is_dir():
                    continue
                seen.add(hit)
                if dry_run:
                    findings.append(Finding("scratch", str(hit), "would-remove"))
                    continue
                try:
                    _rmtree(hit)
                    findings.append(Finding("scratch", str(hit), "removed"))
                except OSError as exc:
                    # ACL-locked sandbox leftovers need an elevated shell —
                    # report, don't crash the sweep.
                    findings.append(
                        Finding("scratch", str(hit), "failed", detail=str(exc))
                    )
                    log.warning("janitor: could not remove %s: %s", hit, exc)
    return findings
