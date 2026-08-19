"""Tiny Telegram Bot API client for long polling and outbound replies.

No SDK. Just the two calls the bridge needs:
- getUpdates for inbound long polling
- sendMessage for replies / scheduled notifications
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from tesseract import http_client
from tesseract.integrations._channel_attachment import ChannelAttachment

_API_BASE = "https://api.telegram.org"
_DEFAULT_TIMEOUT = httpx.Timeout(35.0, connect=10.0)


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    message_id: int
    chat_id: int
    chat_type: str
    from_user_id: int | None
    from_username: str | None
    text: str
    date: int
    # Visibility-first envelope (CR-1): every recognized non-text part
    # surfaces as a ``ChannelAttachment`` so the assistant sees what was sent
    # even when no decoder is wired yet (status="no_handler").
    attachments: tuple[ChannelAttachment, ...] = field(default_factory=tuple)


class TelegramAPIError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, *, base_url: str = _API_BASE) -> None:
        token = token.strip()
        if not token:
            raise ValueError("TelegramAPI requires a bot token")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = http_client.async_client(timeout=_DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe", {})
        return result if isinstance(result, dict) else {}

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int = 25,
        allowed_updates: tuple[str, ...] = ("message",),
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": int(timeout),
            "allowed_updates": list(allowed_updates),
        }
        if offset is not None:
            payload["offset"] = int(offset)
        result = await self._call("getUpdates", payload, read_timeout=timeout + 10)
        if not isinstance(result, list):
            raise TelegramAPIError(f"getUpdates returned {type(result).__name__}")
        return result

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a text message. Link previews land by default (Session 2
        2026-05-16 — "feels like a real person"); per-call opt-out for the
        rare case (progress edits, status pings) where the preview would
        clutter the conversation.

        ``reply_markup`` lets the caller attach an ``inline_keyboard`` —
        used by the channel-gate ASK round-trip (2026-05-17) so the
        operator can approve / reject from their phone instead of needing
        Mirror open. Pass a Telegram Bot API ReplyMarkup dict.
        """
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "text": text,
            "disable_web_page_preview": bool(disable_web_page_preview),
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._call("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramAPIError("sendMessage returned non-object result")
        return result

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        """Dismiss the spinner on a tapped inline keyboard button.

        Telegram blocks the button under a spinner until this is called.
        ``text`` shows a small toast on the user's screen (capped 200
        chars by Telegram); ``show_alert=True`` turns it into a modal
        instead — we use the toast form for "approved" / "rejected"
        confirmation."""
        payload: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = text[:200]
        if show_alert:
            payload["show_alert"] = True
        await self._call("answerCallbackQuery", payload)

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Remove or replace the inline keyboard on an existing message.

        Called after a callback button is tapped so the keyboard
        disappears — keeps the conversation tidy and prevents the
        operator from tapping the same approval twice."""
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._call("editMessageReplyMarkup", payload)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Edit an existing message. Default keeps previews OFF on edits
        because the progress-narrative placeholder churns many times per
        turn (CR-4) and re-fetching previews on each edit would both
        flood Telegram's link cache and replace the placeholder card
        every second. The final reply lands via :meth:`send_message`
        (fresh send, previews on)."""
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": text,
            "disable_web_page_preview": bool(disable_web_page_preview),
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        result = await self._call("editMessageText", payload)
        return result if isinstance(result, dict) else {}

    async def send_chat_action(self, *, chat_id: int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": int(chat_id), "action": action})

    async def set_message_reaction(
        self,
        *,
        chat_id: int,
        message_id: int,
        emoji: str | None,
        is_big: bool = False,
    ) -> None:
        """React to ``message_id`` with a single emoji (Session 3 2026-05-16).

        Passing ``emoji=None`` clears any prior reaction. ``is_big``
        triggers the "burst" animation Telegram uses for first-time
        reactions on important messages — kept off by default so the
        ack pulse stays subtle. Telegram rejects emojis outside the
        bot-allowed set with ``BAD_REQUEST`` (currently 👍👎❤🔥🥰👏
        😁🤔🤯😱🤬😢🎉🤩🤮💩🙏👌🕊🤡🥱🥴😍🐳❤‍🔥🌚🌭💯🤣⚡🍌🏆💔
        🤨😐🍓🍾💋🖕😈😴😭🤓👻👨‍💻👀🎃🙈😇😨🤝✍🤗🫡🎅🎄☃💅🤪
        🗿🆒💘🙉🦄😘💊🙊😎👾🤷‍♂️🤷🤷‍♀️😡); enforcing the list here
        would lag Telegram's quarterly additions, so we forward whatever
        the caller passes and surface the API error verbatim.
        """
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
        }
        if emoji:
            payload["reaction"] = [{"type": "emoji", "emoji": emoji}]
        else:
            payload["reaction"] = []
        if is_big:
            payload["is_big"] = True
        await self._call("setMessageReaction", payload)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        """Resolve a ``file_id`` to a ``file_path`` via ``getFile``."""
        result = await self._call("getFile", {"file_id": file_id})
        if not isinstance(result, dict):
            raise TelegramAPIError("getFile returned non-object result")
        return result

    async def download_file_path(self, file_path: str) -> bytes:
        """Download bytes from the Telegram file CDN for a resolved ``file_path``."""
        url = f"{self._base_url}/file/bot{self._token}/{file_path}"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"file download HTTP error: {exc}") from exc
        if not response.is_success:
            raise TelegramAPIError(
                f"file download failed: HTTP {response.status_code}"
            )
        return response.content

    async def fetch_url(self, url: str, *, timeout: float = 30.0) -> bytes:
        """GET arbitrary URL bytes (Session 2 2026-05-16).

        Public helper so callers don't reach into ``self._client``
        privately. Used by the bridge's ``send_photo(source_url=...)``
        path to pull an ``image_generate`` artifact or external URL
        before forwarding via ``sendPhoto``.
        """
        try:
            response = await self._client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"fetch_url HTTP error: {exc}") from exc
        if not response.is_success:
            raise TelegramAPIError(
                f"fetch_url failed: HTTP {response.status_code}"
            )
        return response.content

    async def send_voice(
        self,
        *,
        chat_id: int,
        ogg_opus_bytes: bytes,
        filename: str = "voice.ogg",
        caption: str | None = None,
        duration_s: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Multipart-upload an OGG/Opus voice note (Session 2 2026-05-16).

        Telegram renders the round voice-note UI only for ``.ogg`` files
        with Opus codec — see :mod:`tesseract.voice.encode` for the WAV→
        OGG/Opus conversion the bridge calls before this API. Sending a
        WAV through ``sendVoice`` produces a generic "audio file" pill
        instead of the voice-note UI.
        """
        data: dict[str, str] = {"chat_id": str(int(chat_id))}
        if caption is not None:
            data["caption"] = caption
        if duration_s is not None:
            data["duration"] = str(int(duration_s))
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(int(reply_to_message_id))
        files = {"voice": (filename, ogg_opus_bytes, "audio/ogg")}
        result = await self._multipart_call("sendVoice", data, files)
        return result if isinstance(result, dict) else {}

    async def send_photo(
        self,
        *,
        chat_id: int,
        image_bytes: bytes,
        filename: str = "photo.jpg",
        mime_type: str = "image/jpeg",
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Multipart-upload an image file.

        ``caption`` is rendered under the photo (1024 char Telegram cap;
        callers should pre-truncate). ``mime_type`` should match the
        bytes — Telegram doesn't enforce strict type-by-bytes matching
        but a mismatch may flip rendering to the generic file UI.
        """
        data: dict[str, str] = {"chat_id": str(int(chat_id))}
        if caption is not None:
            data["caption"] = caption
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(int(reply_to_message_id))
        files = {"photo": (filename, image_bytes, mime_type)}
        result = await self._multipart_call("sendPhoto", data, files)
        return result if isinstance(result, dict) else {}

    async def send_video(
        self,
        *,
        chat_id: int,
        video_bytes: bytes,
        filename: str = "video.mp4",
        mime_type: str = "video/mp4",
        caption: str | None = None,
        duration_s: int | None = None,
        width: int | None = None,
        height: int | None = None,
        supports_streaming: bool = True,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Multipart-upload a video (Session 3 2026-05-16).

        ``supports_streaming=True`` is Telegram's hint that the file is
        in streaming-friendly format (MP4/MOV); recipient clients then
        play it inline rather than downloading first. Width/height/
        duration are advisory — Telegram probes the file regardless,
        but supplying them speeds up the upload-card render.
        """
        data: dict[str, str] = {"chat_id": str(int(chat_id))}
        if caption is not None:
            data["caption"] = caption
        if duration_s is not None:
            data["duration"] = str(int(duration_s))
        if width is not None:
            data["width"] = str(int(width))
        if height is not None:
            data["height"] = str(int(height))
        if supports_streaming:
            data["supports_streaming"] = "true"
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(int(reply_to_message_id))
        files = {"video": (filename, video_bytes, mime_type)}
        result = await self._multipart_call("sendVideo", data, files)
        return result if isinstance(result, dict) else {}

    async def send_video_note(
        self,
        *,
        chat_id: int,
        video_bytes: bytes,
        filename: str = "video_note.mp4",
        duration_s: int | None = None,
        length_px: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Multipart-upload a round video note (Session 3 2026-05-16).

        Telegram's round-video format: ``length_px`` is the square edge
        in pixels (max 640). No caption — Telegram intentionally
        suppresses it for video notes so the round bubble stays clean.
        """
        data: dict[str, str] = {"chat_id": str(int(chat_id))}
        if duration_s is not None:
            data["duration"] = str(int(duration_s))
        if length_px is not None:
            data["length"] = str(int(length_px))
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(int(reply_to_message_id))
        files = {"video_note": (filename, video_bytes, "video/mp4")}
        result = await self._multipart_call("sendVideoNote", data, files)
        return result if isinstance(result, dict) else {}

    async def send_animation(
        self,
        *,
        chat_id: int,
        animation_bytes: bytes,
        filename: str = "animation.mp4",
        mime_type: str = "video/mp4",
        caption: str | None = None,
        duration_s: int | None = None,
        width: int | None = None,
        height: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Multipart-upload a GIF / animation (Session 3 2026-05-16).

        Telegram's ``sendAnimation`` accepts MP4 / GIF; MP4 is the
        on-wire format Telegram clients convert all GIFs to anyway,
        so MP4 is the default mime here for byte-efficiency.
        """
        data: dict[str, str] = {"chat_id": str(int(chat_id))}
        if caption is not None:
            data["caption"] = caption
        if duration_s is not None:
            data["duration"] = str(int(duration_s))
        if width is not None:
            data["width"] = str(int(width))
        if height is not None:
            data["height"] = str(int(height))
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(int(reply_to_message_id))
        files = {"animation": (filename, animation_bytes, mime_type)}
        result = await self._multipart_call("sendAnimation", data, files)
        return result if isinstance(result, dict) else {}

    async def send_sticker(
        self,
        *,
        chat_id: int,
        sticker: str | bytes,
        emoji: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a sticker (Session 3 2026-05-16).

        ``sticker`` is either a Telegram ``file_id`` (string — re-use
        a sticker already on Telegram's CDN) OR raw bytes to upload as
        a one-off WebP. ``emoji`` is the associated emoji rendered
        underneath the sticker in some Telegram clients.
        """
        if isinstance(sticker, str):
            payload: dict[str, Any] = {
                "chat_id": int(chat_id),
                "sticker": sticker,
            }
            if emoji:
                payload["emoji"] = emoji
            if reply_to_message_id is not None:
                payload["reply_to_message_id"] = int(reply_to_message_id)
            result = await self._call("sendSticker", payload)
            return result if isinstance(result, dict) else {}

        data: dict[str, str] = {"chat_id": str(int(chat_id))}
        if emoji:
            data["emoji"] = emoji
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(int(reply_to_message_id))
        files = {"sticker": ("sticker.webp", sticker, "image/webp")}
        result = await self._multipart_call("sendSticker", data, files)
        return result if isinstance(result, dict) else {}

    async def send_location(
        self,
        *,
        chat_id: int,
        latitude: float,
        longitude: float,
        horizontal_accuracy: float | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Share a static location (Session 3 2026-05-16)."""
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "latitude": float(latitude),
            "longitude": float(longitude),
        }
        if horizontal_accuracy is not None:
            payload["horizontal_accuracy"] = float(horizontal_accuracy)
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        result = await self._call("sendLocation", payload)
        return result if isinstance(result, dict) else {}

    async def send_contact(
        self,
        *,
        chat_id: int,
        phone_number: str,
        first_name: str,
        last_name: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Share a contact card (Session 3 2026-05-16)."""
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "phone_number": phone_number,
            "first_name": first_name,
        }
        if last_name is not None:
            payload["last_name"] = last_name
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        result = await self._call("sendContact", payload)
        return result if isinstance(result, dict) else {}

    async def send_poll(
        self,
        *,
        chat_id: int,
        question: str,
        options: list[str],
        is_anonymous: bool = True,
        allows_multiple_answers: bool = False,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a poll (Session 3 2026-05-16).

        2-10 options; Telegram rejects shorter / longer lists.
        """
        if not (2 <= len(options) <= 10):
            raise TelegramAPIError(
                f"sendPoll needs 2-10 options, got {len(options)}"
            )
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "question": question,
            "options": [{"text": o} for o in options],
            "is_anonymous": bool(is_anonymous),
            "allows_multiple_answers": bool(allows_multiple_answers),
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        result = await self._call("sendPoll", payload)
        return result if isinstance(result, dict) else {}

    async def send_dice(
        self,
        *,
        chat_id: int,
        emoji: str = "🎲",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send an animated dice / game (Session 3 2026-05-16).

        Valid emojis: 🎲 (dice 1-6), 🎯 (darts 1-6), 🏀 (basketball
        1-5), ⚽ (football 1-5), 🎰 (slots 1-64), 🎳 (bowling 1-6).
        """
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "emoji": emoji,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        result = await self._call("sendDice", payload)
        return result if isinstance(result, dict) else {}

    async def send_media_group(
        self,
        *,
        chat_id: int,
        media: list[dict[str, Any]],
        reply_to_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Send an album of 2-10 photos/videos in one bubble (Session 3 2026-05-16).

        ``media`` is a list of Telegram InputMediaPhoto / InputMediaVideo
        dicts with their ``media`` field set to either a Telegram
        ``file_id`` (string) or ``"attach://<key>"`` referencing a
        multipart file in the same request. For simplicity, this
        helper supports only the ``file_id`` / public-URL form (no
        per-item multipart upload). Callers who need per-item uploads
        should send individually via ``send_photo`` / ``send_video``.
        """
        if not (2 <= len(media) <= 10):
            raise TelegramAPIError(
                f"sendMediaGroup needs 2-10 items, got {len(media)}"
            )
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "media": media,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        result = await self._call("sendMediaGroup", payload)
        return result if isinstance(result, list) else []

    async def send_document(
        self,
        *,
        chat_id: int,
        document_bytes: bytes,
        filename: str,
        mime_type: str | None = None,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Multipart-upload an arbitrary file as a Telegram document.

        Use for PDFs, text files, exports — anything that's not an image
        or audio file. ``filename`` is the name the recipient sees in
        their chat client.
        """
        data: dict[str, str] = {"chat_id": str(int(chat_id))}
        if caption is not None:
            data["caption"] = caption
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(int(reply_to_message_id))
        files = {"document": (filename, document_bytes, mime_type or "application/octet-stream")}
        result = await self._multipart_call("sendDocument", data, files)
        return result if isinstance(result, dict) else {}

    async def _multipart_call(
        self,
        method: str,
        data: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> Any:
        """POST a multipart/form-data request for upload endpoints.

        ``files`` maps the Bot API field name (``voice`` / ``photo`` /
        ``document``) to ``(filename, bytes, mime_type)`` — httpx packs
        the tuple into the multipart body. Read timeout extended to
        120 s because large uploads on flaky networks need slack beyond
        the default 35 s tuned for ``getUpdates``.
        """
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            response = await self._client.post(
                url, data=data, files=files,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"{method} HTTP error: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramAPIError(
                f"{method} returned non-JSON (HTTP {response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise TelegramAPIError(f"{method} response was not an object")
        if not response.is_success or not payload.get("ok", False):
            desc = payload.get("description") or f"HTTP {response.status_code}"
            raise TelegramAPIError(f"{method} failed: {desc}")
        return payload.get("result")

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        read_timeout: float | None = None,
    ) -> Any:
        timeout = httpx.Timeout(read_timeout, connect=10.0) if read_timeout is not None else None
        url = f"{self._base_url}/bot{self._token}/{method}"
        try:
            response = await self._client.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"{method} HTTP error: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramAPIError(f"{method} returned non-JSON (HTTP {response.status_code})") from exc
        if not isinstance(data, dict):
            raise TelegramAPIError(f"{method} response was not an object")
        if not response.is_success or not data.get("ok", False):
            desc = data.get("description") or f"HTTP {response.status_code}"
            raise TelegramAPIError(f"{method} failed: {desc}")
        return data.get("result")


def parse_message_update(update: dict[str, Any]) -> TelegramMessage | None:
    """Parse a Telegram ``getUpdates`` envelope into a :class:`TelegramMessage`.

    Visibility-first (CR-1): every recognized non-text part surfaces as
    a ``ChannelAttachment`` so the bridge can forward it through the
    ``<channel_attachment>`` envelope. ``None`` is returned only when
    the update is structurally unusable (missing ``update_id``/``chat``
    or non-private chat with no recognized parts); a chat-targeted
    update with no parseable kind still yields a ``TelegramMessage``
    with one ``kind="unknown"`` attachment so the assistant never silently
    misses an addressed message.
    """

    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if not isinstance(chat_id, int) or not isinstance(chat_type, str):
        return None
    from_user = message.get("from") or {}
    from_user_id = from_user.get("id") if isinstance(from_user.get("id"), int) else None
    from_username = (
        from_user.get("username") if isinstance(from_user.get("username"), str) else None
    )
    message_id = message.get("message_id") if isinstance(message.get("message_id"), int) else 0
    date = message.get("date") if isinstance(message.get("date"), int) else 0

    raw_text = message.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    raw_caption = message.get("caption")
    caption = raw_caption.strip() if isinstance(raw_caption, str) else None

    attachments = _extract_attachments(message, caption=caption)
    if not text and not attachments:
        attachments = (
            ChannelAttachment(kind="unknown", status="no_handler", source="telegram"),
        )

    return TelegramMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        from_user_id=from_user_id,
        from_username=from_username,
        text=text,
        date=date,
        attachments=attachments,
    )


def _extract_attachments(
    message: dict[str, Any], *, caption: str | None
) -> tuple[ChannelAttachment, ...]:
    """Map a Telegram message body onto zero-or-more :class:`ChannelAttachment`.

    All emitted attachments carry ``status="no_handler"`` — CR-1 only
    surfaces visibility; CR-2 fills in concrete decoders. The caption is
    attached to the first non-text part so the assistant sees ``user typed X
    alongside the photo`` in one place.
    """
    parts: list[ChannelAttachment] = []
    if isinstance(message.get("voice"), dict):
        parts.append(_voice_attachment(message["voice"]))
    if isinstance(message.get("audio"), dict):
        parts.append(_audio_attachment(message["audio"]))
    if isinstance(message.get("photo"), list) and message["photo"]:
        parts.append(_photo_attachment(message["photo"]))
    if isinstance(message.get("video"), dict):
        parts.append(_video_attachment(message["video"], kind="video"))
    if isinstance(message.get("video_note"), dict):
        parts.append(_video_attachment(message["video_note"], kind="video_note"))
    if isinstance(message.get("animation"), dict):
        parts.append(_video_attachment(message["animation"], kind="animation"))
    if isinstance(message.get("document"), dict):
        parts.append(_document_attachment(message["document"]))
    if isinstance(message.get("sticker"), dict):
        parts.append(_sticker_attachment(message["sticker"]))
    if isinstance(message.get("location"), dict):
        parts.append(_location_attachment(message["location"]))
    if isinstance(message.get("contact"), dict):
        parts.append(_contact_attachment(message["contact"]))
    if isinstance(message.get("poll"), dict):
        parts.append(_poll_attachment(message["poll"]))
    if isinstance(message.get("dice"), dict):
        parts.append(_dice_attachment(message["dice"]))

    if not parts:
        return ()

    if caption:
        head = parts[0]
        # Sticker (and any future kind that pre-populates ``caption``
        # with machine-generated metadata) keeps its descriptor; the
        # user-supplied message caption appends after a separator so
        # The assistant sees both signals without one clobbering the other.
        merged_caption = (
            f"{head.caption}; {caption}" if head.caption else caption
        )
        parts[0] = ChannelAttachment(
            kind=head.kind,
            status=head.status,
            source=head.source,
            mime=head.mime,
            size=head.size,
            duration_s=head.duration_s,
            filename=head.filename,
            width=head.width,
            height=head.height,
            caption=merged_caption,
            ref=head.ref,
        )
    return tuple(parts)


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _voice_attachment(node: dict[str, Any]) -> ChannelAttachment:
    return ChannelAttachment(
        kind="voice",
        status="no_handler",
        source="telegram",
        mime=_as_str(node.get("mime_type")),
        size=_as_int(node.get("file_size")),
        duration_s=_as_int(node.get("duration")),
        ref=_as_str(node.get("file_id")),
    )


def _audio_attachment(node: dict[str, Any]) -> ChannelAttachment:
    return ChannelAttachment(
        kind="audio",
        status="no_handler",
        source="telegram",
        mime=_as_str(node.get("mime_type")),
        size=_as_int(node.get("file_size")),
        duration_s=_as_int(node.get("duration")),
        filename=_as_str(node.get("file_name")),
        ref=_as_str(node.get("file_id")),
    )


def _photo_attachment(sizes: list[Any]) -> ChannelAttachment:
    best: dict[str, Any] | None = None
    best_pixels = -1
    for entry in sizes:
        if not isinstance(entry, dict):
            continue
        width = entry.get("width")
        height = entry.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        pixels = width * height
        if pixels > best_pixels:
            best = entry
            best_pixels = pixels
    if best is None and sizes and isinstance(sizes[-1], dict):
        best = sizes[-1]
    best = best or {}
    return ChannelAttachment(
        kind="photo",
        status="no_handler",
        source="telegram",
        size=_as_int(best.get("file_size")),
        width=_as_int(best.get("width")),
        height=_as_int(best.get("height")),
        ref=_as_str(best.get("file_id")),
    )


def _video_attachment(node: dict[str, Any], *, kind: str) -> ChannelAttachment:
    return ChannelAttachment(
        kind=kind,  # type: ignore[arg-type]
        status="no_handler",
        source="telegram",
        mime=_as_str(node.get("mime_type")),
        size=_as_int(node.get("file_size")),
        duration_s=_as_int(node.get("duration")),
        filename=_as_str(node.get("file_name")),
        width=_as_int(node.get("width")),
        height=_as_int(node.get("height")),
        ref=_as_str(node.get("file_id")),
    )


def _document_attachment(node: dict[str, Any]) -> ChannelAttachment:
    return ChannelAttachment(
        kind="document",
        status="no_handler",
        source="telegram",
        mime=_as_str(node.get("mime_type")),
        size=_as_int(node.get("file_size")),
        filename=_as_str(node.get("file_name")),
        ref=_as_str(node.get("file_id")),
    )


def _sticker_attachment(node: dict[str, Any]) -> ChannelAttachment:
    emoji = _as_str(node.get("emoji"))
    set_name = _as_str(node.get("set_name"))
    parts: list[str] = []
    if emoji:
        parts.append(f"emoji={emoji}")
    if set_name:
        parts.append(f"set={set_name}")
    descriptor = " ".join(parts) if parts else None
    return ChannelAttachment(
        kind="sticker",
        status="no_handler",
        source="telegram",
        size=_as_int(node.get("file_size")),
        width=_as_int(node.get("width")),
        height=_as_int(node.get("height")),
        caption=descriptor,
        ref=_as_str(node.get("file_id")),
    )


def _location_attachment(node: dict[str, Any]) -> ChannelAttachment:
    lat = node.get("latitude")
    lon = node.get("longitude")
    descriptor = None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        descriptor = f"lat={lat} lon={lon}"
    return ChannelAttachment(
        kind="location",
        status="no_handler",
        source="telegram",
        caption=descriptor,
    )


def _contact_attachment(node: dict[str, Any]) -> ChannelAttachment:
    first_name = _as_str(node.get("first_name"))
    last_name = _as_str(node.get("last_name"))
    phone = _as_str(node.get("phone_number"))
    name = " ".join(p for p in (first_name, last_name) if p) or None
    descriptor_parts: list[str] = []
    if name:
        descriptor_parts.append(f"name={name}")
    if phone:
        descriptor_parts.append(f"phone={phone}")
    descriptor = " ".join(descriptor_parts) if descriptor_parts else None
    return ChannelAttachment(
        kind="contact",
        status="no_handler",
        source="telegram",
        caption=descriptor,
    )


def _poll_attachment(node: dict[str, Any]) -> ChannelAttachment:
    question = _as_str(node.get("question"))
    return ChannelAttachment(
        kind="poll",
        status="no_handler",
        source="telegram",
        caption=question,
    )


def _dice_attachment(node: dict[str, Any]) -> ChannelAttachment:
    emoji = _as_str(node.get("emoji"))
    value = node.get("value")
    descriptor_parts: list[str] = []
    if emoji:
        descriptor_parts.append(f"emoji={emoji}")
    if isinstance(value, int):
        descriptor_parts.append(f"value={value}")
    descriptor = " ".join(descriptor_parts) if descriptor_parts else None
    return ChannelAttachment(
        kind="dice",
        status="no_handler",
        source="telegram",
        caption=descriptor,
    )
