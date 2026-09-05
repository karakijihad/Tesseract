"""Process sweep — reap orphans the runtime launched.

Kill rule (fixed here, not configurable): a process dies only when it is
an orphan — its parent is gone (psutil guards pid reuse via create_time)
— AND it is identified as ours. Always skipped, regardless of orphanhood:
the janitor's own ancestry, pids claimed via `<home>/run/*.pid`
(detached-by-design supervisor/backend, see pidfile.py), and a controller
daemon with a fresh heartbeat.

Identity comes in three rungs, narrowest first:

1. **The mark.** `TESSERACT_REAPABLE`, written into the environment of
   every CLI the runtime spawns (`cli_utils.py::mark_reapable`) and read
   back with `psutil.Process().environ()`. A marked orphan is ours no
   matter what its command line says, which is what closes the leak: a
   seat pointed at a CLI nobody wrote a pattern for still gets reaped.
2. **The derived CLI fingerprints**, built from `providers.yaml`'s
   `cli.*.command` values so no CLI binary is named twice. These fire
   ONLY when the environment could not be read at all — an unreadable
   environment must degrade to the old regex behaviour, never to "not
   ours, leave it running". Where it CAN be read, unmarked means foreign,
   and the operator's own `claude -p` in a terminal survives the sweep.
3. **The hand-written patterns in `janitor.yaml`**, which describe
   processes the runtime did not launch and therefore cannot mark. They
   are matched regardless of the mark."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable

import psutil

from tesseract.kernel.adapters.cli_utils import REAPABLE_ENV

from .config import JanitorConfig
from .models import Finding
from .pidfile import claimed_pids

log = logging.getLogger(__name__)

_TERMINATE_GRACE_S = 3.0


def _command_pattern(command: str) -> str:
    """Match `command` where it stands as the executable of a command line.

    A whole whitespace-delimited token that is the command itself or a path
    ending in it, with the Windows extensions npm and the installers add.
    Broad enough to cover every real invocation (that is what the degraded
    rung owes), and narrow enough that a `--config ...\.claude\settings.json`
    argument is not a match.
    """
    return (
        r'(?:^|[\s"])(?:[^\s"]*[\\/])?'
        + re.escape(command)
        + r'(?:\.exe|\.cmd|\.bat)?"?(?=\s|$)'
    )


def _derived_cli_fingerprints() -> list[tuple[str, re.Pattern[str]]]:
    """One fingerprint per `providers.yaml` cli command — the only place a
    CLI binary is named. Read fresh: a provider added at 14:00 is covered by
    the 15:15 sweep.

    An unreadable catalog costs the degraded rung and nothing else — the mark
    needs no config to be read — so it is logged rather than raised, which
    would take the whole sweep down over a file this half does not own.
    """
    try:
        from tesseract.config.loader import cli_commands, load_config

        commands = cli_commands(load_config().providers_raw)
    except Exception:  # noqa: BLE001 — the mark path is unaffected
        log.warning(
            "janitor: providers.yaml unreadable; sweeping without the derived "
            "CLI fingerprints. Marked orphans are still reaped."
        )
        return []
    return [(f"cli-{c}", re.compile(_command_pattern(c))) for c in sorted(commands)]


def _reapable_owner(proc: psutil.Process) -> tuple[str | None, bool]:
    """`(owner, readable)` from the process's environment.

    `readable=False` is not evidence the process is foreign: `environ()`
    raises for another user's processes and is unimplemented in some
    sandboxes. The caller degrades to the fingerprints there.
    """
    try:
        return proc.environ().get(REAPABLE_ENV), True
    except (psutil.Error, OSError, NotImplementedError):
        return None, False


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
    foreign = [(fp.id, re.compile(fp.pattern)) for fp in cfg.process_fingerprints]
    blind = foreign + _derived_cli_fingerprints()
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
            # Orphanhood first: it is the cheaper half of the kill rule, and
            # reading an environment is not free on a live process table.
            if not _is_orphan(proc):
                continue
            owner, readable = _reapable_owner(proc)
            if owner:
                hit: str | None = f"mark:{owner}"
            else:
                hit = next(
                    (
                        fid
                        for fid, rx in (foreign if readable else blind)
                        if rx.search(cmdline)
                    ),
                    None,
                )
            if hit is None:
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
