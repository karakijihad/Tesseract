"""Console capture for supervisor-spawned daemons (backend, controller).

The supervisor historically spawned its children with inherited stdio,
which in the packaged app (console-less Tauri GUI) means *discarded*
stdio: an import-time crash printed a traceback to nowhere and the
supervisor could only log ``backend exited (code=1)``. That is exactly
how the 2026-07-29 pywinpty crash storm shipped undiagnosable.

This module drains a child's merged stdout/stderr pipe into a rotating
``<runtime>/logs/<name>-console.log`` (sizes from
``mirror.yaml::logging`` via :func:`tesseract.logsetup.load_logging_config`
— single source of truth, no defaults here) and keeps an in-memory tail
so the supervisor can inline the child's last words into its own log on
a crash exit.

The child's *durable* logging is still its own ``attach_file_logging``
file — this capture exists for the window before that is armed (import
errors, config errors) and for anything that only ever hits the console.
"""

from __future__ import annotations

import collections
import logging
import subprocess
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from tesseract.logsetup import load_logging_config
from tesseract.paths import runtime_logs_root

log = logging.getLogger(__name__)

TAIL_LINES = 40


class ConsoleWriter:
    """One rotating console log for a named child. Lives across respawns
    (one file handle per supervisor process, not per spawn) so rotation
    counts the whole history, and the tail can span a respawn boundary.
    """

    def __init__(self, name: str) -> None:
        cfg = load_logging_config()
        # `runtime/`, not `<home>/<logging.dir>`. These are console captures of
        # machine-ops processes — the backend, the supervisor, the agent
        # controller — and `migrate_install_layout` has classified
        # `*-console.log` as runtime-side since the split shipped. Building the
        # path from the home root put them back on the SYNCED half after every
        # migration, exactly as `logsetup.py` did until it was corrected; this
        # was the other half of the same defect and outlived the first fix.
        #
        # The home is deliberately NOT a parameter. It was one, unread, for
        # long enough that the signature implied a choice the class does not
        # offer: the target resolves from the environment at call time, so a
        # caller passing a different home would have been silently ignored.
        path = runtime_logs_root() / f"{name}-console.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handler = RotatingFileHandler(
            path,
            maxBytes=cfg["max_bytes"],
            backupCount=cfg["backup_count"],
            encoding="utf-8",
        )
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self._lock = threading.Lock()
        self.tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)

    def write_line(self, line: str) -> None:
        self.tail.append(line)
        record = logging.LogRecord(
            name="console", level=logging.INFO, pathname="", lineno=0,
            msg=line, args=(), exc_info=None,
        )
        with self._lock:
            try:
                self._handler.emit(record)
            except Exception:  # noqa: BLE001 — diagnostics must never kill the supervisor
                pass

    def tail_text(self) -> str:
        return "\n".join(self.tail)


def popen_capture_kwargs() -> dict:
    """Popen kwargs that merge the child's stderr into a drainable stdout
    pipe. Callers layer these over their own kwargs (env, flags, stdin).
    """
    return {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}


def start_drain(proc: subprocess.Popen, writer: ConsoleWriter, name: str) -> None:
    """Start a daemon thread that copies ``proc``'s merged output into
    ``writer`` until EOF. No-op when the process has no stdout pipe
    (separate-console dev mode, test fakes).
    """
    stream = getattr(proc, "stdout", None)
    if stream is None:
        return
    writer.write_line(f"--- {name} spawned pid={proc.pid} ---")

    def _drain() -> None:
        try:
            for raw in iter(stream.readline, b""):
                writer.write_line(
                    raw.decode("utf-8", errors="replace").rstrip("\r\n")
                )
        except Exception:  # noqa: BLE001 — pipe torn down mid-read on kill
            log.debug("console drain for %s ended abnormally", name, exc_info=True)
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_drain, name=f"console-drain-{name}", daemon=True).start()


__all__ = ["ConsoleWriter", "popen_capture_kwargs", "start_drain", "TAIL_LINES"]
