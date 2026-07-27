"""Generic circuit breaker.

MAX_FAILURES consecutive failures → trip. Tripped breakers are logged
to logs/circuit-breakers/{name}.jsonl. Session continues without the
broken subsystem (degradation, not failure).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml

from tesseract.paths import CONFIG_DIR

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _default_max_failures() -> int:
    cfg_path = CONFIG_DIR / "providers.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return int(cfg["availability"]["max_consecutive_failures"])


class CircuitBreaker:
    def __init__(self, name: str, max_failures: int | None = None, log_dir: Path | None = None) -> None:
        if max_failures is None:
            max_failures = _default_max_failures()
        self.name = name
        self.max_failures = max_failures
        self.failure_count = 0
        self.is_tripped = False
        self._log_dir = log_dir
        self._rehydrate_from_log()

    def _rehydrate_from_log(self) -> None:
        # Without this, a fresh process loses the persisted "tripped" state:
        # the next successful call short-circuits at `if self.is_tripped:` in
        # record_success and never appends a "reset" event, leaving the
        # JSONL (and the mirror UI that reads it) stuck open forever.
        if self._log_dir is None:
            return
        log_path = self._log_dir / f"{self.name}.jsonl"
        if not log_path.exists():
            return
        last_event: str | None = None
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    last_event = json.loads(line).get("event")
                except json.JSONDecodeError:
                    continue
        if last_event == "tripped":
            self.is_tripped = True
            self.failure_count = self.max_failures

    def record_success(self) -> None:
        self.failure_count = 0
        if self.is_tripped:
            self.is_tripped = False
            logger.info("Circuit breaker '%s' auto-reset after successful call", self.name)
            self._log_reset()

    def record_failure(self, error: str = "") -> None:
        self.failure_count += 1
        if self.failure_count >= self.max_failures and not self.is_tripped:
            self.is_tripped = True
            logger.warning("Circuit breaker '%s' tripped after %d failures", self.name, self.failure_count)
            self._log_trip(error)
            try:
                from tesseract.orchestrator.background_event_bus import get_background_bus
                get_background_bus().publish(
                    "breaker_trip",
                    {
                        "name": self.name,
                        "failure_count": self.failure_count,
                        "error": error[:200],
                    },
                )
            except Exception:
                pass

    def reset(self) -> None:
        was_tripped = self.is_tripped
        self.failure_count = 0
        self.is_tripped = False
        if was_tripped:
            try:
                from tesseract.orchestrator.background_event_bus import get_background_bus
                get_background_bus().publish(
                    "breaker_reset",
                    {"name": self.name},
                )
            except Exception:
                pass

    def _log_trip(self, error: str) -> None:
        if self._log_dir is None:
            return
        log_path = self._log_dir / f"{self.name}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "event": "tripped",
            "breaker": self.name,
            "failures": self.failure_count,
            "error": error,
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _log_reset(self) -> None:
        if self._log_dir is None:
            return
        log_path = self._log_dir / f"{self.name}.jsonl"
        entry = {
            "event": "reset",
            "breaker": self.name,
            "timestamp": datetime.now().astimezone().isoformat(),
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def load_breaker_statuses(log_dir: Path) -> list[dict]:
    """Return full status of every persisted circuit breaker.

    Each entry: {name, tripped, tripped_at, error, reset_at}.
    """
    if not log_dir.exists():
        return []
    statuses = []
    for log_file in sorted(log_dir.glob("*.jsonl")):
        events: list[dict] = []
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        last_event = events[-1] if events else None
        last_trip = next((e for e in reversed(events) if e.get("event") == "tripped"), None)
        last_reset = next((e for e in reversed(events) if e.get("event") == "reset"), None)
        statuses.append({
            "name": log_file.stem,
            "tripped": bool(last_event and last_event.get("event") == "tripped"),
            "tripped_at": last_trip.get("timestamp") if last_trip else None,
            "error": last_trip.get("error", "") if last_trip else None,
            "reset_at": last_reset.get("timestamp") if last_reset else None,
        })
    return statuses


def load_tripped_breakers(log_dir: Path) -> dict[str, bool]:
    """Scan circuit breaker logs and return which breakers are currently tripped.

    Returns {breaker_name: True} for breakers with a trip event and no
    subsequent reset. Used at session startup to restore breaker state.
    """
    if not log_dir.exists():
        return {}
    tripped: dict[str, bool] = {}
    for log_file in log_dir.glob("*.jsonl"):
        breaker_name = log_file.stem
        last_event = None
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    last_event = json.loads(line)
                except json.JSONDecodeError:
                    continue
        if last_event and last_event.get("event") == "tripped":
            tripped[breaker_name] = True
    return tripped
