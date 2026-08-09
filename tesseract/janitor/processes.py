"""Process sweep — reap fingerprinted orphans.

Kill rule (fixed here, not configurable): a process dies only when its
command line matches a configured fingerprint AND it is an orphan —
its parent is gone (psutil guards pid reuse via create_time). Always
skipped, regardless of orphanhood: the janitor's own ancestry, pids
claimed via `<home>/run/*.pid` (detached-by-design supervisor/backend,
see pidfile.py), and a controller daemon with a fresh heartbeat."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable

import psutil

from .config import JanitorConfig
from .models import Finding
from .pidfile import claimed_pids

log = logging.getLogger(__name__)

_TERMINATE_GRACE_S = 3.0


def _own_ancestry() -> set[int]:
    pids = {psutil.Process().pid}
    try:
        for anc in psutil.Process().parents():
            pids.add(anc.pid)
    except psutil.Error:
        pass
    return pids


def _controller_claim(cfg: JanitorConfig) -> set[int]:
    """The controller daemon is detached by design; a fresh heartbeat
    file (path published in controller.json) is its claim."""
    from tesseract.paths import TESSERACT_HOME, runtime_dir

    env = os.environ.get("TESSERACT_HOME")
    home = Path(env).resolve() if env else TESSERACT_HOME
    meta_path = runtime_dir() / "agent_controller" / "controller.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        pid = int(meta["pid"])
        heartbeat = Path(meta["heartbeat_path"])
        age_s = time.time() - heartbeat.stat().st_mtime
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return set()
    if age_s <= cfg.claimed_heartbeat_max_age_s:
        return {pid}
    return set()


def _is_orphan(proc: psutil.Process) -> bool:
    try:
        parent = proc.parent()  # None on dead/reused ppid (create_time check)
    except psutil.Error:
        return False
    return parent is None or not parent.is_running()


def sweep_processes(
    cfg: JanitorConfig,
    *,
    dry_run: bool,
    procs: Iterable[psutil.Process] | None = None,
) -> list[Finding]:
    """`procs` is injectable for tests; defaults to the live process table."""
    patterns = [(fp.id, re.compile(fp.pattern)) for fp in cfg.process_fingerprints]
    skip = _own_ancestry() | claimed_pids() | _controller_claim(cfg)
    findings: list[Finding] = []
    to_kill: list[tuple[psutil.Process, str, str]] = []

    for proc in procs if procs is not None else psutil.process_iter():
        try:
            if proc.pid in skip:
                continue
            cmdline = " ".join(proc.cmdline())
            if not cmdline:
                continue
            hit = next((fid for fid, rx in patterns if rx.search(cmdline)), None)
            if hit is None or not _is_orphan(proc):
                continue
            target = f"pid={proc.pid} [{hit}] {cmdline[:120]}"
            if dry_run:
                findings.append(Finding("processes", target, "would-kill"))
            else:
                to_kill.append((proc, hit, target))
        except psutil.Error:
            continue  # exited mid-scan or access denied — not ours to touch

    if to_kill:
        for proc, _, _ in to_kill:
            try:
                proc.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(
            [p for p, _, _ in to_kill], timeout=_TERMINATE_GRACE_S
        )
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass
        for proc, _, target in to_kill:
            findings.append(Finding("processes", target, "killed"))
            log.info("janitor: reaped orphan %s", target)

    return findings
