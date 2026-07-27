"""Circuit breaker for the stateful observer.

Diverges from `tesseract/context/circuit_breaker.py` in one way: this
variant auto-closes after a 60 s cooldown so a transient adapter outage
doesn't leave the observer dead until disarm. Consolidation with the
shared class is a candidate for Phase 4 once disarm semantics land.
"""

from __future__ import annotations

import time

from tesseract.context.circuit_breaker import _default_max_failures

COOLDOWN_SECONDS = 60.0


class CircuitBreaker:
    def __init__(
        self,
        max_consecutive_failures: int | None = None,
        cooldown_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        if max_consecutive_failures is None:
            max_consecutive_failures = _default_max_failures()
        self._max = max_consecutive_failures
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._max and self._opened_at is None:
            self._opened_at = time.monotonic()

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            self._failures = 0
            self._opened_at = None
            return False
        return True

    def state(self) -> str:
        """green / yellow (partial failures) / red (breaker open)."""
        if self.is_open():
            return "red"
        if self._failures > 0:
            return "yellow"
        return "green"

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None
