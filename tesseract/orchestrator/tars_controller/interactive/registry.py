from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any


class InteractiveSessionRegistry:
    """Per-ChatSession registry of open interactive sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}

    def mint_handle(self, target: str) -> str:
        slug = target.replace("_", "-")
        ts = datetime.now(timezone.utc).strftime("%H%M%S")
        return f"{slug}-{ts}-{secrets.token_hex(3)}"

    def add(self, session: Any) -> None:
        self._sessions[session.handle] = session

    def get(self, handle: str) -> Any | None:
        return self._sessions.get(handle)

    def list(self) -> list[Any]:
        return list(self._sessions.values())

    def remove(self, handle: str) -> None:
        self._sessions.pop(handle, None)

    async def close_all(self) -> None:
        for s in list(self._sessions.values()):
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
