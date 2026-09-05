"""Shared file-logging pipeline for TESSERACT's long-lived processes.

The supervisor spawns the Mirror backend and the agent controller with
inherited console streams (deliberate — CTRL_BREAK shutdown semantics,
see ``supervisor/daemon.py``), so ``logging.basicConfig`` output vanishes
with the console and a crash-respawn eats the evidence. This module gives
each process durable rotating files under ``runtime/logs/`` without
touching the console handler — one per boot, plus a rolling aggregate.

Config lives in ``tesseract/config/mirror.yaml::logging`` (single source
of truth — missing keys raise at boot). ``file_min_levels`` sets
per-logger floors that apply to the FILE only, so request-per-poll noise
(``aiohttp.access``, ``httpx``) stays on the console but out of the
durable log; errors from those loggers still land in the file.

IO failures are fail-soft: a locked/unwritable log file must not take
the assistant down. Config errors are fail-loud, per project rule.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping

import yaml

from tesseract.bootid import current_boot_id
from tesseract.lib.log_envelope import BAD, envelope
from tesseract.lib.secret_patterns import CREDENTIAL_PATTERNS
from tesseract.paths import CONFIG_DIR, log_dir, runtime_logs_root

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


def _rotating_handler(path: Path, cfg: Mapping[str, Any]) -> RotatingFileHandler | None:
    """A configured handler at ``path``, or ``None`` if it cannot be opened.

    Fail-soft by design: losing a log file must never stop the process that
    was trying to write it.
    """
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
            "file logging unavailable at %s — continuing without it", path
        )
        return None
    handler.setFormatter(logging.Formatter(_FORMAT))
    if cfg["file_min_levels"]:
        handler.addFilter(_PerLoggerFloorFilter(cfg["file_min_levels"]))
    return handler


def boot_log_path(process: str) -> Path:
    """Where this process's per-boot log lives, without opening it.

    ``runtime/logs/backend/<process>-<boot id>.log``. The boot id leads with
    ``YYYYMMDDTHHMMSS``, so a plain name sort puts the newest run last and
    "which file is this run" is answerable from outside the process.
    """
    return log_dir("backend") / f"{process}-{current_boot_id()}.log"



class _EnvelopeHandler(logging.Handler):
    """Every ERROR the backend logs, written a second time as a record.

    The backend's log is plain text and stays that way. Third-party libraries
    log into it, its lines carry a local wall clock with no zone, and the
    level lives inside the text rather than in a field. Making it structured
    would mean owning the output of every dependency in the tree, which is not
    a thing to own.

    So it gains structured events BESIDE the text rather than instead of it.
    The text file remains the forensic record, whole and uninterleaved, and
    this writes one enveloped line per ERROR into a stream every other reader
    can treat like the four that are structured at the source. What used to
    happen instead: the watchman reopened every per-boot log, tailed it,
    matched a regex for the level, and inferred a subject from the logger
    name, which is seven of the fifteen findings on the traced pass coming
    from the one stream that could say nothing about itself.

    **It may not raise and it may not recurse.** A handler that logs on
    failure is a handler that can loop, so a write that fails is dropped
    silently. The text line is already on disk by then, so nothing is lost
    that the operator could not still read.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(level=logging.ERROR)
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            row = envelope(
                stream="backend",
                # ERROR and CRITICAL are both "something a person looks at".
                # The distinction between them is the module author's and it
                # stays in `detail` rather than becoming a third severity
                # nothing downstream branches on.
                severity=BAD,
                subject=f"{record.levelname} from {record.name}",
                # The runtime's own words: a level and a logger name, both
                # chosen by whoever wrote the module. The MESSAGE is whatever
                # the process was handed and it is not in the summary, which
                # is the same line the report draws between what it composed
                # and what it quoted.
                summary=f"{record.name} logged an error",
                detail={
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage()[:2000],
                },
                ts=datetime.fromtimestamp(record.created, tz=timezone.utc),
            )
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - a logging handler never raises
            pass


def backend_events_path(process: str) -> Path:
    """Where this process's structured errors land, beside its text log."""
    return log_dir("backend") / f"{process}-{current_boot_id()}.events.jsonl"


def attach_file_logging(process: str, *, config_path: Path = MIRROR_YAML) -> Path | None:
    """Attach this process's file logging. Call once at start, after
    ``basicConfig``.

    Two handlers, because they answer different questions:

    * ``runtime/logs/backend/<process>-<boot id>.log`` — THIS run, whole and
      uninterleaved. What "show me that launch" needs.
    * ``runtime/logs/<process>.log`` — the rolling aggregate across boots,
      kept as the backstop it has always been. Size rotation bounds both.

    Returns the per-boot path, or ``None`` when it could not be opened — in
    which case the AGGREGATE handler may still be attached, since it is armed
    first. ``None`` therefore means "no per-boot file", not "no file logging".

    ``logging.dir`` does not govern either of these paths; both resolve through
    ``paths.py``'s home-vs-runtime ownership split. The key is still required
    and still live — ``supervisor/console_capture.py`` builds the console-log
    paths from it — so it is inconsistently honored rather than dead, and that
    is worth knowing before someone edits it expecting these files to move.
    """
    cfg = load_logging_config(config_path)
    root = logging.getLogger()

    # `runtime/`, not `home/`: these are machine ops. `migrate_install_layout`
    # has classified them that way since the split shipped, but this function
    # built its path from TESSERACT_HOME directly and so recreated the file on
    # the home side after every migration — putting machine-ops logs on the
    # SYNCED side of the boundary that `paths.log_dir` exists to hold.
    aggregate = runtime_logs_root() / f"{process}.log"
    if (handler := _rotating_handler(aggregate, cfg)) is not None:
        root.addHandler(handler)

    per_boot = boot_log_path(process)
    if (handler := _rotating_handler(per_boot, cfg)) is None:
        logging.getLogger(__name__).info("file logging armed at %s", aggregate)
        return None
    root.addHandler(handler)
    root.addHandler(_EnvelopeHandler(backend_events_path(process)))
    logging.getLogger(__name__).info(
        "file logging armed — this boot at %s, aggregate at %s", per_boot, aggregate
    )
    return per_boot


_REDACTED = "[redacted]"


def scrub_log_text(text: str) -> str:
    """Blank both kinds of credential in a line headed for a log or a surface.

    Public because the filter below is not the only thing that needs it.
    `mirror/server/log_forwarder.py` builds its own payload from the RAW
    exception object rather than from what the filter rewrote, and a filter
    cannot reach that: a filter can only mutate the record, and nothing it
    writes touches `record.exc_info`. Attaching the filter to that handler
    would not have fixed it either, which is why this is a function both call
    rather than a filter one of them wears.

    Two kinds, answering different questions. The patterns are SHAPES and
    catch a provider token nobody registered. The store's values are EXACT
    and catch the assistant's own accounts, whose tokens may look like
    nothing in particular.

    It never raises. Every caller is on a logging path, including the records
    that report the credential store is unreadable, so an exception here takes
    the log tree down with it.
    """
    try:
        for pattern in CREDENTIAL_PATTERNS:
            text = pattern.sub(_REDACTED, text)
    except Exception:  # noqa: BLE001 — a bad pattern must not break logging
        return text
    try:
        from tesseract.credentials.redaction import redact

        return redact(text)
    except Exception:  # noqa: BLE001 — no credential layer, or it could not read
        return text


class _CredentialRedactionFilter(logging.Filter):
    """Redact provider credentials from a record before any handler emits it.

    The record this was written for is `httpx`'s per-request INFO line. The
    Telegram Bot API puts the token in the URL PATH (``/bot<TOKEN>/getUpdates``),
    so a polling install printed its bot token on the console once per poll —
    every terminal, redirect, CI job and screen recording that captures stdout.
    `mirror.yaml::logging.file_min_levels` floors `httpx` for the FILE, which is
    why the durable logs were clean and the console was not.

    Flooring `httpx` on the console too was the narrower alternative and was not
    taken: it would throw away request logging that is genuinely useful in dev
    to fix a leak that is about the VALUE, not the logger.

    Two things are covered, and they answer different questions. The patterns
    are shapes, and they catch a provider token nobody registered anywhere. The
    store's own values are exact, and they catch the assistant's own accounts —
    an account whose token has no recognisable prefix is invisible to a pattern
    and obvious to an exact match.

    **The traceback is covered too**, which it was not before. This filter used
    to rewrite the record's MESSAGE and say in this docstring that an attached
    exception was formatted by the handler afterwards and left alone. That is
    the likeliest leak of the two: a request that fails with the credential in
    a header raises with the header in a frame. The exception is formatted here
    and the result parked on `record.exc_text`, which is exactly what
    `logging.Formatter` reads before it would format one itself.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a bad format string is the handler's to report
            return True
        redacted = self._scrub(message)
        if redacted != message:
            # `args` are already interpolated into `redacted`; leaving them set
            # would make the handler interpolate a second time and raise.
            record.msg = redacted
            record.args = ()
        self._scrub_traceback(record)
        if isinstance(record.stack_info, str) and record.stack_info:
            record.stack_info = self._scrub(record.stack_info)
        return True

    @staticmethod
    def _scrub(text: str) -> str:
        return scrub_log_text(text)

    def _scrub_traceback(self, record: logging.LogRecord) -> None:
        """Format the exception now, scrubbed, so the handler uses ours.

        `Formatter.format` only calls `formatException` when `exc_text` is
        empty, so filling it is how a filter reaches a traceback at all. Set
        only when scrubbing changed something, so a formatter with its own
        `formatException` keeps it on every ordinary record.
        """
        if record.exc_text:
            scrubbed = self._scrub(record.exc_text)
            if scrubbed != record.exc_text:
                record.exc_text = scrubbed
            return
        if not record.exc_info:
            return
        try:
            formatted = "".join(traceback.format_exception(*record.exc_info))
        except Exception:  # noqa: BLE001 — a broken exc_info is the handler's to report
            return
        if formatted.endswith("\n"):
            formatted = formatted[:-1]
        scrubbed = self._scrub(formatted)
        if scrubbed != formatted:
            record.exc_text = scrubbed


def redact_credentials_in_logs() -> None:
    """Attach the redaction filter to every handler on the root logger.

    On the HANDLERS rather than on a logger: a filter on a logger sees only
    records logged directly to it, and every leak this exists for is emitted by
    a third-party logger propagating upward. Call once at process start, after
    the console and file handlers are attached — anything added later is not
    covered.
    """
    redactor = _CredentialRedactionFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


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
    "boot_log_path",
    "load_logging_config",
    "redact_credentials_in_logs",
    "suppress_proactor_disconnect_noise",
    "MIRROR_YAML",
]
