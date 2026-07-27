"""Stale controller-session sweep.

A session record stuck `status="active"` whose owning controller daemon
is dead (pid gone) and whose `last_active_at` is past the grace window
gets `status="closed"` written back — the 2026-07-13 incident left two
such shells inflating the activity map. `"closed"` is a valid
`SessionInfo.status` literal (protocol.py)."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

from .config import JanitorConfig
from .models import Finding

log = logging.getLogger(__name__)


def _home() -> Path:
    from tesseract.paths import TESSERACT_HOME

    env = os.environ.get("TESSERACT_HOME")
    return Path(env).resolve() if env else TESSERACT_HOME


def _controller_alive(controller_dir: Path, max_heartbeat_age_s: int) -> bool:
    """Same liveness signals as processes.py::_controller_claim: a fresh
    heartbeat file, or the recorded pid still running something
    Tesseract-shaped (pid-reuse guard — bare pid_exists would freeze the
    sweep forever when an unrelated process recycles the pid)."""
    meta = controller_dir / "controller.json"
    try:
        record = json.loads(meta.read_text(encoding="utf-8"))
        pid = int(record["pid"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False
    try:
        heartbeat = Path(record["heartbeat_path"])
        if time.time() - heartbeat.stat().st_mtime <= max_heartbeat_age_s:
            return True
    except (OSError, KeyError, TypeError):
        pass
    try:
        proc = psutil.Process(pid)
        return "tesseract" in " ".join(proc.cmdline()).lower()
    except psutil.Error:
        return False


def sweep_sessions(cfg: JanitorConfig, *, dry_run: bool) -> list[Finding]:
    controller_dir = _home() / "tars_controller"
    sessions_dir = controller_dir / "sessions"
    if not sessions_dir.is_dir():
        return []
    if _controller_alive(controller_dir, cfg.claimed_heartbeat_max_age_s):
        return []  # daemon owns its sessions — never touch them while it lives

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=cfg.stale_session_grace_hours
    )
    findings: list[Finding] = []
    for path in sessions_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(Finding("sessions", path.name, "failed", detail=str(exc)))
            continue
        if record.get("status") != "active":
            continue
        last_active = record.get("last_active_at") or record.get("created_at") or ""
        try:
            stamp = datetime.fromisoformat(str(last_active).replace("Z", "+00:00"))
        except ValueError:
            stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if stamp > cutoff:
            continue
        target = f"{record.get('session_id', path.stem)} ({last_active or 'no stamp'})"
        if dry_run:
            findings.append(Finding("sessions", target, "would-close"))
            continue
        record["status"] = "closed"
        try:
            # tmp + os.replace, matching SessionRegistry's atomic-write
            # pattern — a crash mid-write must not truncate the record.
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
            findings.append(Finding("sessions", target, "closed"))
            log.info("janitor: closed stale controller session %s", target)
        except OSError as exc:
            findings.append(Finding("sessions", target, "failed", detail=str(exc)))
    return findings
