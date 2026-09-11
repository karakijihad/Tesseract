"""Reaching Telegram from a process that never built the bridge.

The bridge lives in the Mirror backend. Two things that need to speak are not
it: a scheduler run with no backend session, and the supervisor at the moment
it stops trying to restart the backend at all. Both have the token in the
environment and both have the channel's own files on disk, which is everything
this channel needs to say something.

So this builds the same surface an adapter has, `list_users` and `send_text`,
out of `<TESSERACT_HOME>/telegram/allowlist.json` and `state.json` plus
`TelegramAPI`. Whoever gets it then goes through `notify_operators` like every
other caller: who hears from the runtime is answered in one place whether the
bridge is up or not.

It is deliberately NOT registered as a channel. A registered adapter is one the
backend is running and polling; this one can only speak, never listen, and a
surface that lists it as live would be lying.
"""

from __future__ import annotations

import logging
import os
import re
from html import unescape
from pathlib import Path
from typing import Any

from tesseract.integrations._channel_adapter import ChannelUser
from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)


def state_dir() -> Path:
    return (Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
            / "telegram")


def _strip_html_tags(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text))


class OfflineTelegram:
    """Enough of the adapter surface for one message to go out."""

    name = "telegram"

    def __init__(self, api: Any, users: list[ChannelUser]) -> None:
        self._api = api
        self._users = users

    def list_users(self) -> list[ChannelUser]:
        return list(self._users)

    def render_message(self, message: Any) -> str:
        from tesseract.integrations.telegram.render import render_telegram

        return render_telegram(message)

    async def send_text(
        self, *, chat_ref: str, text: str,
        disable_web_page_preview: bool = False,
    ) -> str:
        """The first chunk's message id, or `""` when nothing was sent.

        The same answer the bridge gives, because this is the same surface and
        a second implementation that quietly returned nothing would make a
        message sent while the backend was down the one message nobody could
        check. That is the path where it matters most: the supervisor speaks
        here at the moment it has stopped trying to restart anything.
        """
        from tesseract.integrations.telegram.api import TelegramAPIError
        from tesseract.integrations.telegram.bridge import _message_id_of
        from tesseract.integrations.telegram.chunker import chunk_for_telegram

        chat_id = int(chat_ref)
        first_id: int | None = None
        for chunk in chunk_for_telegram(text or ""):
            try:
                result = await self._api.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=disable_web_page_preview,
                )
            except TelegramAPIError:
                # The tags are what failed, not the words. Retried without
                # them so the message still arrives.
                log.warning(
                    "telegram: HTML send failed for chat=%s; retrying plain",
                    chat_ref,
                )
                result = await self._api.send_message(
                    chat_id=chat_id,
                    text=_strip_html_tags(chunk),
                    disable_web_page_preview=disable_web_page_preview,
                )
            if first_id is None:
                first_id = _message_id_of(result)
        return "" if first_id is None else str(first_id)

    async def aclose(self) -> None:
        try:
            await self._api.aclose()
        except Exception:  # noqa: BLE001
            log.exception("telegram: closing the offline client failed")


def build() -> OfflineTelegram | None:
    """The adapter, or None when this machine cannot speak Telegram.

    Every reason to return None is a missing prerequisite the caller cannot
    do anything about: no token, no allowlist, no client. None of them is an
    error worth raising into a process that is already handling something
    else.
    """
    # Which env var holds the credential is `channels.yaml::telegram.api_key_env`,
    # the same resolution the bridge does. Reading a name written into this
    # file instead meant renaming it in the config silently disarmed the one
    # path that speaks when the backend is gone, since a missing token here is
    # a quiet no-op by design.
    from tesseract.integrations._channels_config import channel_key_env

    token = (os.environ.get(channel_key_env("telegram")) or "").strip()
    if not token:
        return None

    try:
        from tesseract.integrations.telegram.api import TelegramAPI
        from tesseract.integrations.telegram.state import load_allowlist, load_state
    except Exception:
        log.exception("telegram: offline client import failed")
        return None

    root = state_dir()
    try:
        allowlist = load_allowlist(
            root / "allowlist.json",
            env_seed=os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"),
        )
    except Exception:
        log.exception("telegram: could not read who is allowed")
        return None

    users = _users_from(allowlist)
    if not users:
        return None

    try:
        api = TelegramAPI(token)
    except Exception:
        log.exception("telegram: offline client init failed")
        return None
    return OfflineTelegram(api, users)


def _users_from(allowlist: Any) -> list[ChannelUser]:
    """The allowlist files as the roster every other caller reads.

    Projecting its own users is the one thing only the channel can do, so it
    is done here, from the same two files the bridge reads. Which of them
    hears from the runtime stays where it is, in `_outbound`.
    """

    def _user(chat_id: Any, state: str) -> ChannelUser:
        key = str(chat_id)
        return ChannelUser(
            user_id=key,
            display_name=key,
            ttl_iso=None,
            first_seen="",
            last_seen="",
            messages_total=0,
            state=state,  # type: ignore[arg-type]
        )

    out = [_user(cid, "allowed") for cid in sorted(getattr(allowlist, "chat_ids", set()))]
    out += [
        _user(cid, "pending")
        for cid in sorted((getattr(allowlist, "pending", {}) or {}).keys())
    ]
    out += [_user(cid, "blocked") for cid in sorted(getattr(allowlist, "blocked", set()))]
    return out


__all__ = ["OfflineTelegram", "build", "state_dir"]
