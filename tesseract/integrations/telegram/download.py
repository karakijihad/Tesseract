"""Telegram attachment fetcher — ``getFile`` + CDN download with cap checks (CR-2A)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from tesseract.integrations.telegram.api import TelegramAPI, TelegramAPIError

log = logging.getLogger(__name__)


_BOT_API_HARD_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MiB — Telegram Bot API ceiling


FetchFailure = Literal["too_large", "missing_ref", "fetch_failed"]


@dataclass(frozen=True)
class FetchedAttachment:
    """Successful download — caller dispatches on ``mime`` if needed."""

    data: bytes
    size: int


@dataclass(frozen=True)
class FetchRejection:
    """Structured failure; bridge maps ``kind`` onto ``ChannelAttachment.status``."""

    kind: FetchFailure
    detail: str


async def fetch_telegram_attachment(
    file_id: str | None,
    *,
    api: TelegramAPI,
    max_bytes: int | None,
) -> FetchedAttachment | FetchRejection:
    """Resolve ``file_id`` → bytes, honoring ``max_bytes`` and the Bot API 20 MiB ceiling."""
    if not file_id:
        return FetchRejection(kind="missing_ref", detail="attachment has no file_id")

    try:
        meta = await api.get_file(file_id)
    except TelegramAPIError as exc:
        log.warning("telegram getFile failed for file_id=%s: %s", file_id, exc)
        return FetchRejection(kind="fetch_failed", detail=str(exc))

    raw_size = meta.get("file_size")
    size = raw_size if isinstance(raw_size, int) else 0

    hard_cap = _BOT_API_HARD_LIMIT_BYTES
    effective_cap = min(max_bytes, hard_cap) if max_bytes else hard_cap
    if size and size > effective_cap:
        return FetchRejection(
            kind="too_large",
            detail=f"{size} bytes exceeds cap {effective_cap}",
        )

    file_path = meta.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return FetchRejection(
            kind="fetch_failed",
            detail="getFile returned no file_path (file > 20 MiB or expired)",
        )

    try:
        data = await api.download_file_path(file_path)
    except TelegramAPIError as exc:
        log.warning("telegram file download failed for file_id=%s: %s", file_id, exc)
        return FetchRejection(kind="fetch_failed", detail=str(exc))

    if max_bytes is not None and len(data) > max_bytes:
        return FetchRejection(
            kind="too_large",
            detail=f"{len(data)} bytes exceeds cap {max_bytes}",
        )

    return FetchedAttachment(data=data, size=len(data))


__all__ = [
    "FetchedAttachment",
    "FetchRejection",
    "FetchFailure",
    "fetch_telegram_attachment",
]
