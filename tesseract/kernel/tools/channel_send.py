"""Outbound channel media tools (Session 2 2026-05-16).

Three sibling tools — ``channel_send_voice`` / ``channel_send_photo`` /
``channel_send_document`` — that the assistant calls when replying with audio,
images, or files on an external chat channel (Telegram today, future
WhatsApp / Signal via the same protocol).

Each tool:
1. Resolves the live adapter via ``integrations.get_channel(channel)``.
2. Dispatches to the adapter's ``send_voice`` / ``send_photo`` /
   ``send_document`` methods (which handle TTS synthesis, WAV→OGG
   conversion, path-vs-bytes-vs-URL resolution, and outbound persistence).
3. Returns a one-line summary of what was sent so the assistant sees confirmation
   in its tool transcript.

``default_posture="auto"`` — the assistant replying mid-conversation is the
expected behavior, not a gated escalation. ``permissions.yaml`` can flip
to ``ask`` per-tool if the operator wants a confirmation step.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


def _resolve_adapter(channel: str):
    """Late import — `tesseract.integrations` imports adapters that pull
    aiohttp + Telegram code; deferring keeps tool boot cheap."""
    from tesseract.integrations import get_channel
    adapter = get_channel(channel)
    if adapter is None:
        raise RuntimeError(
            f"channel '{channel}' is not registered "
            "(adapter not built or disabled in channels.yaml)"
        )
    return adapter


# -- channel_send_voice -------------------------------------------------


class ChannelSendVoiceInput(BaseModel):
    channel: str = Field(default="telegram", description="Channel name (telegram today).")
    chat_ref: str = Field(description="Channel-native chat identifier (Telegram chat_id as string).")
    text: Optional[str] = Field(
        default=None,
        description=(
            "Text to speak. Synthesised via the TTS lane "
            "roles.yaml::voice.tts names, then transcoded "
            "to OGG/Opus so Telegram renders the voice-note UI."
        ),
    )
    audio_path: Optional[str] = Field(
        default=None,
        description=(
            "Optional path to a pre-rendered OGG/Opus file. Use only "
            "when the operator hands you a specific recording — TTS is "
            "preferred for natural conversation."
        ),
    )
    caption: Optional[str] = Field(
        default=None,
        description="Optional caption shown beside the voice note.",
    )
    reply_to_message_id: Optional[int] = Field(
        default=None,
        description="Telegram message_id to quote-reply to.",
    )


class ChannelSendVoiceTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Send a spoken voice note on an external chat channel."
    use_when: ClassVar[str] = (
        "Pass `text` for TTS synthesis (default, conversational) or `audio_path` for "
        "a pre-rendered audio file. Renders as a playable voice-note bubble."
    )
    not_when: ClassVar[str] = (
        "the content is a recording someone should download and keep — use "
        "`channel_send_document` for that."
    )

    @property
    def name(self) -> str:
        return "channel_send_voice"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelSendVoiceInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelSendVoiceInput)
            else ChannelSendVoiceInput(**tool_input.model_dump())
        )
        if (inp.text is None) == (inp.audio_path is None):
            return ToolResult(
                output="channel_send_voice: pass exactly one of `text` or `audio_path`",
                is_error=True,
            )
        try:
            adapter = _resolve_adapter(inp.channel)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)

        audio_bytes: bytes | None = None
        if inp.audio_path is not None:
            from pathlib import Path
            p = Path(inp.audio_path)
            if not p.is_file():
                return ToolResult(
                    output=f"channel_send_voice: audio_path is not a file: {inp.audio_path}",
                    is_error=True,
                )
            # Run on the executor so a large pre-rendered audio file
            # doesn't block the event loop (reviewer C5 — matches the
            # bridge's `_resolve_media_source` pattern).
            audio_bytes = await asyncio.get_event_loop().run_in_executor(
                None, p.read_bytes,
            )

        try:
            result = await adapter.send_voice(
                chat_ref=inp.chat_ref,
                text=inp.text,
                audio_bytes=audio_bytes,
                caption=inp.caption,
                reply_to_message_id=inp.reply_to_message_id,
            )
        except Exception as exc:
            return ToolResult(output=f"channel_send_voice failed: {exc}", is_error=True)

        msg_id = result.get("message_id") if isinstance(result, dict) else None
        source = "TTS" if inp.text is not None else f"file ({inp.audio_path})"
        return ToolResult(
            output=f"sent voice to {inp.channel}:{inp.chat_ref} from {source} (message_id={msg_id})",
        )


# -- channel_send_photo -------------------------------------------------


class ChannelSendPhotoInput(BaseModel):
    channel: str = Field(default="telegram", description="Channel name (telegram today).")
    chat_ref: str = Field(description="Channel-native chat identifier.")
    source_path: Optional[str] = Field(
        default=None,
        description=(
            "Path to an image file on disk. Use for files in the vault "
            "(`vault/raw/...`), workspace, or downloads tree. Operator-"
            "controlled paths only — the bridge does not sandbox arbitrary "
            "paths."
        ),
    )
    source_url: Optional[str] = Field(
        default=None,
        description=(
            "HTTP(S) URL the bridge fetches and forwards. Useful for "
            "image_generate output URLs and external sources."
        ),
    )
    caption: Optional[str] = Field(
        default=None,
        description="Optional caption shown under the photo (1024 char cap).",
    )
    reply_to_message_id: Optional[int] = Field(
        default=None,
        description="Telegram message_id to quote-reply to.",
    )


class ChannelSendPhotoTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Send an image inline on an external chat channel."
    use_when: ClassVar[str] = (
        "Source is either `source_path` (vault/workspace/downloads file) or "
        "`source_url` (typically an `image_generate` output URL). Renders inline "
        "with an optional caption."
    )
    not_when: ClassVar[str] = (
        "the operator needs the original file at full quality — `channel_send_document` "
        "preserves it but drops the inline preview."
    )

    @property
    def name(self) -> str:
        return "channel_send_photo"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelSendPhotoInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelSendPhotoInput)
            else ChannelSendPhotoInput(**tool_input.model_dump())
        )
        if (inp.source_path is None) == (inp.source_url is None):
            return ToolResult(
                output="channel_send_photo: pass exactly one of `source_path` or `source_url`",
                is_error=True,
            )
        try:
            adapter = _resolve_adapter(inp.channel)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)

        try:
            result = await adapter.send_photo(
                chat_ref=inp.chat_ref,
                source_path=inp.source_path,
                source_url=inp.source_url,
                caption=inp.caption,
                reply_to_message_id=inp.reply_to_message_id,
            )
        except Exception as exc:
            return ToolResult(output=f"channel_send_photo failed: {exc}", is_error=True)

        msg_id = result.get("message_id") if isinstance(result, dict) else None
        source = inp.source_path or inp.source_url
        return ToolResult(
            output=f"sent photo to {inp.channel}:{inp.chat_ref} from {source} (message_id={msg_id})",
        )


# -- channel_send_document ----------------------------------------------


class ChannelSendDocumentInput(BaseModel):
    channel: str = Field(default="telegram", description="Channel name (telegram today).")
    chat_ref: str = Field(description="Channel-native chat identifier.")
    source_path: str = Field(
        description=(
            "Path to the file. Operator-controlled paths only — the "
            "bridge does not sandbox arbitrary paths."
        ),
    )
    filename: Optional[str] = Field(
        default=None,
        description="Display name for the recipient. Defaults to source filename.",
    )
    caption: Optional[str] = Field(
        default=None,
        description="Optional caption shown under the file (1024 char cap).",
    )
    reply_to_message_id: Optional[int] = Field(
        default=None,
        description="Telegram message_id to quote-reply to.",
    )


class ChannelSendDocumentTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Send a file as a downloadable document on an external chat channel."
    use_when: ClassVar[str] = (
        "Reads `source_path` and forwards it, any MIME type, so the operator can save "
        "it on the far side of a channel — a Mirror URL is broken there, so a file "
        "going to a channel must be sent this way, the opposite of `open`, which is "
        "for a file the operator already has inside the Mirror."
    )
    not_when: ClassVar[str] = (
        "the file is an image, video, animation, or audio clip that should render "
        "inline instead — use the matching media verb (`channel_send_photo` and siblings)."
    )

    @property
    def name(self) -> str:
        return "channel_send_document"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelSendDocumentInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelSendDocumentInput)
            else ChannelSendDocumentInput(**tool_input.model_dump())
        )
        try:
            adapter = _resolve_adapter(inp.channel)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)

        try:
            result = await adapter.send_document(
                chat_ref=inp.chat_ref,
                source_path=inp.source_path,
                filename=inp.filename,
                caption=inp.caption,
                reply_to_message_id=inp.reply_to_message_id,
            )
        except FileNotFoundError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except Exception as exc:
            return ToolResult(output=f"channel_send_document failed: {exc}", is_error=True)

        msg_id = result.get("message_id") if isinstance(result, dict) else None
        return ToolResult(
            output=f"sent document to {inp.channel}:{inp.chat_ref} from {inp.source_path} (message_id={msg_id})",
        )


# -- channel_send_video / animation / video_note (Session 3) ----------


def _make_media_tool(
    *, name_: str, adapter_method: str, kind_label: str,
    summary_text: str, use_when_text: str, not_when_text: str,
    allow_no_caption: bool = False,
):
    """Factory for video / animation / video_note tools (Session 3 2026-05-16).

    Each shares the same source-mux + dispatch shape; only the adapter
    method name and the kind label change. Factored to keep the file
    flat instead of repeating 60 lines per kind.

    **Maintenance note:** ``sync_permissions --write`` discovers tools by
    parsing the module's AST for ``def name(self): return "<literal>"``.
    Factory-built tools have ``def name(self): return name_`` (a closure
    reference, not a literal), so the script silently misses them. Any
    new tool built via this factory must be added to
    ``tesseract/config/permissions.yaml::tools:`` by hand. If you find
    yourself adding more than a couple, consider switching to explicit
    subclasses so the auto-sync recovers.
    """

    class _Input(BaseModel):
        channel: str = Field(default="telegram")
        chat_ref: str = Field(description="Channel-native chat identifier.")
        source_path: Optional[str] = Field(default=None, description="Path to the file on disk.")
        source_url: Optional[str] = Field(default=None, description="HTTP URL the bridge will fetch and forward.")
        caption: Optional[str] = Field(default=None, description="Optional caption.")
        reply_to_message_id: Optional[int] = Field(default=None)

    class _Tool(Tool):
        default_posture = "auto"

        risk_class: ClassVar[str] = "autonomous"

        group: ClassVar[str] = "reaching-the-operator"
        summary: ClassVar[str] = summary_text
        use_when: ClassVar[str] = use_when_text
        not_when: ClassVar[str] = not_when_text

        @property
        def name(self) -> str:
            return name_

        @property
        def input_schema(self) -> type[BaseModel]:
            return _Input

        def is_concurrency_safe(self) -> bool:
            return True

        def is_read_only(self) -> bool:
            return False

        def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
            return PermissionResult.PASSTHROUGH

        async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
            inp = (
                tool_input if isinstance(tool_input, _Input)
                else _Input(**tool_input.model_dump())
            )
            if (inp.source_path is None) == (inp.source_url is None):
                return ToolResult(
                    output=f"{name_}: pass exactly one of source_path or source_url",
                    is_error=True,
                )
            try:
                adapter = _resolve_adapter(inp.channel)
            except RuntimeError as exc:
                return ToolResult(output=str(exc), is_error=True)
            method = getattr(adapter, adapter_method, None)
            if method is None:
                return ToolResult(
                    output=f"{name_}: adapter does not support {adapter_method}",
                    is_error=True,
                )
            kwargs: dict = {
                "chat_ref": inp.chat_ref,
                "source_path": inp.source_path,
                "source_url": inp.source_url,
                "reply_to_message_id": inp.reply_to_message_id,
            }
            if not allow_no_caption:
                kwargs["caption"] = inp.caption
            try:
                result = await method(**kwargs)
            except Exception as exc:
                return ToolResult(output=f"{name_} failed: {exc}", is_error=True)
            msg_id = result.get("message_id") if isinstance(result, dict) else None
            source = inp.source_path or inp.source_url
            return ToolResult(
                output=f"sent {kind_label} to {inp.channel}:{inp.chat_ref} from {source} (message_id={msg_id})",
            )

    _Tool.__name__ = "ChannelSend" + kind_label.capitalize().replace("_", "") + "Tool"
    return _Tool


ChannelSendVideoTool = _make_media_tool(
    name_="channel_send_video", adapter_method="send_video", kind_label="video",
    summary_text="Send a video file on an external chat channel.",
    use_when_text=(
        "Source is `source_path` (local file) or `source_url` (HTTP URL). Plays "
        "inline when the format is streaming-friendly."
    ),
    not_when_text=(
        "the clip is a short round bubble — use `channel_send_video_note`; or a "
        "looping clip with no sound — use `channel_send_animation`."
    ),
)

ChannelSendAnimationTool = _make_media_tool(
    name_="channel_send_animation", adapter_method="send_animation", kind_label="animation",
    summary_text="Send a looping GIF-style animation on an external chat channel.",
    use_when_text=(
        "Source is `source_path` (local file) or `source_url` (HTTP URL). A "
        "canonical looping animation format is preferred; raw GIFs are also accepted."
    ),
    not_when_text=(
        "the clip has sound or is meant to play once through — use `channel_send_video` instead."
    ),
)

ChannelSendVideoNoteTool = _make_media_tool(
    name_="channel_send_video_note", adapter_method="send_video_note", kind_label="video_note",
    summary_text="Send a round short-form video-note bubble on an external chat channel.",
    use_when_text=(
        "Use for a quick face-to-camera style clip in the circular bubble format. "
        "No caption is sent alongside it."
    ),
    not_when_text=(
        "a caption matters or the clip is not meant to render as the round bubble — "
        "use `channel_send_video` instead."
    ),
    allow_no_caption=True,
)


# -- channel_send_sticker -------------------------------------------------


class ChannelSendStickerInput(BaseModel):
    channel: str = Field(default="telegram")
    chat_ref: str = Field(description="Channel-native chat identifier.")
    sticker_file_id: Optional[str] = Field(
        default=None,
        description=(
            "Telegram file_id for a sticker already on the CDN — fastest "
            "path; re-use the same sticker many times. Find IDs via the "
            "@stickerbothelperbot or by forwarding a sticker to your bot."
        ),
    )
    sticker_path: Optional[str] = Field(
        default=None,
        description="Path to a local WebP file to upload as a one-off sticker.",
    )
    emoji: Optional[str] = Field(
        default=None,
        description="Optional emoji rendered under the sticker on some clients.",
    )
    reply_to_message_id: Optional[int] = Field(default=None)


class ChannelSendStickerTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Send a sticker on an external chat channel."
    use_when: ClassVar[str] = (
        "Pass `sticker_file_id` for a known CDN sticker (fast) or `sticker_path` for "
        "a local file. Use for emotional punctuation that matches the conversational tone."
    )
    not_when: ClassVar[str] = (
        "the reaction is to a specific message already sent — use `channel_react` instead "
        "of sending a new sticker message."
    )

    @property
    def name(self) -> str:
        return "channel_send_sticker"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelSendStickerInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelSendStickerInput)
            else ChannelSendStickerInput(**tool_input.model_dump())
        )
        if (inp.sticker_file_id is None) == (inp.sticker_path is None):
            return ToolResult(
                output="channel_send_sticker: pass exactly one of sticker_file_id or sticker_path",
                is_error=True,
            )
        try:
            adapter = _resolve_adapter(inp.channel)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        sticker: str | bytes
        if inp.sticker_file_id is not None:
            sticker = inp.sticker_file_id
        else:
            from pathlib import Path
            p = Path(inp.sticker_path)  # type: ignore[arg-type]
            if not p.is_file():
                return ToolResult(
                    output=f"channel_send_sticker: sticker_path not a file: {inp.sticker_path}",
                    is_error=True,
                )
            sticker = await asyncio.get_event_loop().run_in_executor(
                None, p.read_bytes,
            )
        try:
            result = await adapter.send_sticker(
                chat_ref=inp.chat_ref, sticker=sticker, emoji=inp.emoji,
                reply_to_message_id=inp.reply_to_message_id,
            )
        except Exception as exc:
            return ToolResult(output=f"channel_send_sticker failed: {exc}", is_error=True)
        msg_id = result.get("message_id") if isinstance(result, dict) else None
        return ToolResult(
            output=f"sent sticker to {inp.channel}:{inp.chat_ref} (message_id={msg_id})",
        )


# -- channel_send_location ------------------------------------------------


class ChannelSendLocationInput(BaseModel):
    channel: str = Field(default="telegram")
    chat_ref: str = Field(description="Channel-native chat identifier.")
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    reply_to_message_id: Optional[int] = Field(default=None)


class ChannelSendLocationTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Share a static map location on an external chat channel."
    use_when: ClassVar[str] = (
        "Use when the operator asks about a place and a map pin is the clearest answer "
        "('where is the cafe', 'show me the airport')."
    )
    not_when: ClassVar[str] = (
        "a text answer with an address or directions is clearer than a pin."
    )

    @property
    def name(self) -> str:
        return "channel_send_location"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelSendLocationInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelSendLocationInput)
            else ChannelSendLocationInput(**tool_input.model_dump())
        )
        try:
            adapter = _resolve_adapter(inp.channel)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        try:
            result = await adapter.send_location(
                chat_ref=inp.chat_ref,
                latitude=inp.latitude, longitude=inp.longitude,
                reply_to_message_id=inp.reply_to_message_id,
            )
        except Exception as exc:
            return ToolResult(output=f"channel_send_location failed: {exc}", is_error=True)
        msg_id = result.get("message_id") if isinstance(result, dict) else None
        return ToolResult(
            output=f"sent location to {inp.channel}:{inp.chat_ref} (lat={inp.latitude}, lon={inp.longitude}, message_id={msg_id})",
        )


# -- channel_send_poll ----------------------------------------------------


class ChannelSendPollInput(BaseModel):
    channel: str = Field(default="telegram")
    chat_ref: str = Field(description="Channel-native chat identifier.")
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=10)
    is_anonymous: bool = Field(default=True)
    allows_multiple_answers: bool = Field(default=False)
    reply_to_message_id: Optional[int] = Field(default=None)


class ChannelSendPollTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Send a multiple-choice poll on an external chat channel."
    use_when: ClassVar[str] = (
        "Useful for quick decisions with a handful of options ('lunch options?', "
        "'which milestone do you want shipped first?')."
    )
    not_when: ClassVar[str] = (
        "the question is open-ended or has only one sensible answer — `channel_notify` "
        "carries a plain question just as well."
    )

    @property
    def name(self) -> str:
        return "channel_send_poll"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelSendPollInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelSendPollInput)
            else ChannelSendPollInput(**tool_input.model_dump())
        )
        try:
            adapter = _resolve_adapter(inp.channel)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        try:
            result = await adapter.send_poll(
                chat_ref=inp.chat_ref, question=inp.question,
                options=inp.options, is_anonymous=inp.is_anonymous,
                allows_multiple_answers=inp.allows_multiple_answers,
                reply_to_message_id=inp.reply_to_message_id,
            )
        except Exception as exc:
            return ToolResult(output=f"channel_send_poll failed: {exc}", is_error=True)
        msg_id = result.get("message_id") if isinstance(result, dict) else None
        return ToolResult(
            output=f"sent poll to {inp.channel}:{inp.chat_ref} ({len(inp.options)} options, message_id={msg_id})",
        )


# -- channel_react --------------------------------------------------------


class ChannelReactInput(BaseModel):
    channel: str = Field(default="telegram")
    chat_ref: str = Field(description="Channel-native chat identifier.")
    message_id: int = Field(description="Telegram message_id to react to.")
    emoji: Optional[str] = Field(
        default=None,
        description=(
            "Single emoji from Telegram's bot-allowed set (👍👎❤🔥🥰👏😁🤔"
            "🤯😱🤬😢🎉🤩💯🤣⚡🍌🏆🤝✍🤗🫡🆒💘🦄💊). Pass None / "
            "omit to CLEAR a prior reaction."
        ),
    )


class ChannelReactTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "React to an existing channel message with a single emoji."
    use_when: ClassVar[str] = (
        "Use as a lightweight acknowledgment on a specific message without sending a "
        "full reply. Pass `emoji=null` to clear a prior reaction."
    )
    not_when: ClassVar[str] = (
        "the ack needs its own words or is not tied to one existing message — send a "
        "sticker or a `channel_notify` instead."
    )

    @property
    def name(self) -> str:
        return "channel_react"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelReactInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelReactInput)
            else ChannelReactInput(**tool_input.model_dump())
        )
        try:
            adapter = _resolve_adapter(inp.channel)
        except RuntimeError as exc:
            return ToolResult(output=str(exc), is_error=True)
        method = getattr(adapter, "react_to_message", None)
        if method is None:
            return ToolResult(
                output=f"channel_react: adapter does not support react_to_message",
                is_error=True,
            )
        try:
            await method(
                chat_ref=inp.chat_ref, message_id=inp.message_id, emoji=inp.emoji,
            )
        except Exception as exc:
            return ToolResult(output=f"channel_react failed: {exc}", is_error=True)
        action = f"reacted {inp.emoji}" if inp.emoji else "cleared reaction"
        return ToolResult(
            output=f"{action} on {inp.channel}:{inp.chat_ref}#{inp.message_id}",
        )


__all__ = [
    "ChannelSendVoiceTool",
    "ChannelSendPhotoTool",
    "ChannelSendDocumentTool",
    "ChannelSendVideoTool",
    "ChannelSendAnimationTool",
    "ChannelSendVideoNoteTool",
    "ChannelSendStickerTool",
    "ChannelSendLocationTool",
    "ChannelSendPollTool",
    "ChannelReactTool",
]
