"""Shared file-logging pipeline for TESSERACT's long-lived processes.

The supervisor spawns the Mirror backend and the TARS controller with
inherited console streams (deliberate — CTRL_BREAK shutdown semantics,
see ``supervisor/daemon.py``), so ``logging.basicConfig`` output vanishes
with the console and a crash-respawn eats the evidence. This module gives
each process a durable rotating file under ``<TESSERACT_HOME>/logs/``
without touching the console handler.

Config lives in ``tesseract/config/mirror.yaml::logging`` (single source
of truth — missing keys raise at boot). ``file_min_levels`` sets
per-logger floors that apply to the FILE only, so request-per-poll noise
(``aiohttp.access``, ``httpx``) stays on the console but out of the
durable log; errors from those loggers still land in the file.

IO failures are fail-soft: a locked/unwritable log file must not take
the assistant down. Config errors are fail-loud, per project rule.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping

import yaml

from tesseract.paths import CONFIG_DIR, TESSERACT_HOME

MIRROR_YAML = CONFIG_DIR / "mirror.yaml"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _PerLoggerFloorFilter(logging.Filter):
    """Drop records from configured loggers below their floor level.

    Floors match the logger itself or any descendant (standard dotted
    hierarchy), longest prefix wins.
    """

    def __init__(self, floors: Mapping[str, int]) -> None:
        super().__init__()
        self._floors = dict(floors)

    def filter(self, record: logging.LogRecord) -> bool:
        best: str | None = None
        for name in self._floors:
            if record.name == name or record.name.startswith(name + "."):
                if best is None or len(name) > len(best):
                    best = name
        if best is None:
            return True
        return record.levelno >= self._floors[best]


def load_logging_config(path: Path = MIRROR_YAML) -> dict[str, Any]:
    """Strict read of the ``logging:`` block. Raises on missing/malformed
    keys — config is single source of truth, no silent defaults."""
    if not path.exists():
        raise FileNotFoundError(f"config missing: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"config {path} did not parse to a mapping")
    block = raw.get("logging")
    if not isinstance(block, dict):
        raise RuntimeError(f"{path} missing required 'logging' block")
    try:
        dir_rel = str(block["dir"])
        max_bytes = int(block["max_bytes"])
        backup_count = int(block["backup_count"])
        floors_raw = block["file_min_levels"]
    except KeyError as exc:
        raise RuntimeError(f"{path} logging.* missing key: {exc.args[0]}") from exc
    if max_bytes <= 0 or backup_count < 1:
        raise RuntimeError(f"{path} logging.max_bytes/backup_count must be positive")
    if not isinstance(floors_raw, dict):
        raise RuntimeError(f"{path} logging.file_min_levels must be a mapping")
    floors: dict[str, int] = {}
    for name, level_name in floors_raw.items():
        level = logging.getLevelName(str(level_name).upper())
        if not isinstance(level, int):
            raise RuntimeError(
                f"{path} logging.file_min_levels.{name}: unknown level {level_name!r}"
            )
        floors[str(name)] = level
    return {
        "dir": dir_rel,
        "max_bytes": max_bytes,
        "backup_count": backup_count,
        "file_min_levels": floors,
    }


def attach_file_logging(process: str, *, config_path: Path = MIRROR_YAML) -> Path | None:
    """Attach a rotating ``<TESSERACT_HOME>/<dir>/<process>.log`` handler to
    the root logger. Call once at process start, after ``basicConfig``.

    Returns the log path, or ``None`` when the file could not be opened
    (fail-soft: the process keeps running on console logging alone).
    """
    cfg = load_logging_config(config_path)
    home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
    path = home / cfg["dir"] / f"{process}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=cfg["max_bytes"],
            backupCount=cfg["backup_count"],
            encoding="utf-8",
        )
    except OSError:
        logging.getLogger(__name__).exception(
            "file logging unavailable at %s — continuing on console only", path
        )
        return None
    handler.setFormatter(logging.Formatter(_FORMAT))
    if cfg["file_min_levels"]:
        handler.addFilter(_PerLoggerFloorFilter(cfg["file_min_levels"]))
    logging.getLogger().addHandler(handler)
    logging.getLogger(__name__).info("file logging armed at %s", path)
    return path


class _ProactorDisconnectFilter(logging.Filter):
    """Drop ONE known-benign CPython artifact, nothing else.

    On Windows' Proactor event loop, a client (the Tauri webview, a
    browser tab) dropping its WebSocket/HTTP connection abruptly makes
    asyncio log ``Exception in callback
    _ProactorBasePipeTransport._call_connection_lost(None)`` with a
    ``ConnectionResetError`` during transport cleanup. Nothing is wrong —
    the peer just went away — but it lands at ERROR level and pollutes
    every error surface. The match is deliberately narrow (that callback
    name AND a connection-reset class); real asyncio errors pass through
    untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and isinstance(
            record.exc_info[1], (ConnectionResetError, ConnectionAbortedError)
        ):
            if "_call_connection_lost" in record.getMessage():
                return False
        return True


def suppress_proactor_disconnect_noise() -> None:
    """Attach the artifact filter to the ``asyncio`` logger (Windows only —
    the Proactor loop doesn't exist elsewhere). Call once at process start."""
    if sys.platform == "win32":
        logging.getLogger("asyncio").addFilter(_ProactorDisconnectFilter())


__all__ = [
    "attach_file_logging",
    "load_logging_config",
    "suppress_proactor_disconnect_noise",
    "MIRROR_YAML",
]
