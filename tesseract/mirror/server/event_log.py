from __future__ import annotations

from collections import deque
from typing import Any

MAX_EVENTS_PER_SESSION = 5000


class EventLog:
    def __init__(self, maxlen: int = MAX_EVENTS_PER_SESSION) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def append(self, envelope: dict[str, Any]) -> None:
        self._buf.append(envelope)

    def since(self, iso_timestamp: str | None, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        if iso_timestamp is None:
            return list(self._buf)[-limit:]
        out: list[dict[str, Any]] = []
        for env in self._buf:
            if env.get("timestamp", "") > iso_timestamp:
                out.append(env)
                if len(out) >= limit:
                    break
        return out
