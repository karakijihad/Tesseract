"""Archive + opt-in log pruning.

Lane archives: `controller/lanes-archive/<YYYY-MM>/<lane>/` dirs whose
newest file is older than the retention window are removed. Log pruning
runs ONLY over the globs listed in `janitor.yaml` (`log_prune.globs`),
matched under BOTH log roots — `<home>/logs/` and `<runtime>/logs/`. The
docstring said home-relative for as long as the split has existed, which
is wrong in the direction that matters: the machine-ops half is the half
that grows.

`backend/*.log*` is the one entry on by default — per-boot backend logs
accrue one file per launch and are machine ops, not operator history.
Everything else under `logs/` stays untouched unless listed."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .config import JanitorConfig
from .models import Finding
from .scratch import _rmtree

log = logging.getLogger(__name__)

_DAY_S = 86400.0


def _home() -> Path:
    from tesseract.paths import TESSERACT_HOME

    env = os.environ.get("TESSERACT_HOME")
    return Path(env).resolve() if env else TESSERACT_HOME


def _newest_mtime(root: Path) -> float:
    newest = root.stat().st_mtime
    for p in root.rglob("*"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def sweep_archives(cfg: JanitorConfig, *, dry_run: bool) -> list[Finding]:
    findings: list[Finding] = []
    now = time.time()

    archive_root = _home() / "controller" / "lanes-archive"
    archive_cutoff = now - cfg.archive_retention_days * _DAY_S
    if archive_root.is_dir():
        for lane_dir in sorted(archive_root.glob("*/*")):
            if not lane_dir.is_dir():
                continue
            try:
                if _newest_mtime(lane_dir) >= archive_cutoff:
                    continue
                if dry_run:
                    findings.append(
                        Finding("archives", str(lane_dir), "would-prune")
                    )
                    continue
                _rmtree(lane_dir)
                findings.append(Finding("archives", str(lane_dir), "pruned"))
                log.info("janitor: pruned lane archive %s", lane_dir)
            except OSError as exc:
                findings.append(
                    Finding("archives", str(lane_dir), "failed", detail=str(exc))
                )
        # Drop now-empty month dirs so the tree stays readable.
        if not dry_run:
            for month_dir in archive_root.glob("*"):
                try:
                    if month_dir.is_dir() and not any(month_dir.iterdir()):
                        month_dir.rmdir()
                except OSError:
                    pass

    # Both halves of the split tree. Sweeping only one would silently stop
    # pruning the other, and the machine-ops half is the one that grows.
    from tesseract.paths import home_logs_root, runtime_logs_root

    logs_roots = (home_logs_root(), runtime_logs_root())
    log_cutoff = now - cfg.log_prune.retention_days * _DAY_S
    for glob in cfg.log_prune.globs:
        for hit in (h for root in logs_roots for h in root.glob(glob)):
            try:
                if not hit.is_file() or hit.stat().st_mtime >= log_cutoff:
                    continue
                if dry_run:
                    findings.append(Finding("archives", str(hit), "would-prune"))
                    continue
                hit.unlink()
                findings.append(Finding("archives", str(hit), "pruned"))
            except OSError as exc:
                findings.append(
                    Finding("archives", str(hit), "failed", detail=str(exc))
                )
    return findings
