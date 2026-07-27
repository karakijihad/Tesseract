"""TranscribeAudioTool — transcribe a previously-uploaded audio attachment.

The Mirror's WS preprocessor already auto-transcribes audio uploads before
chat_brain sees them (see ``tesseract/mirror/server/ws.py``
``_preprocess_audio_attachments``). This tool covers the secondary case
where chat_brain wants to re-transcribe an existing audio attachment from
its history — for example after the operator says "what did the speaker
say at the end of that file?" and chat_brain wants the raw transcript
again rather than re-reading the prior transcript stub.

Local Whisper engine is preloaded at Mirror startup; this tool just calls
through to it. ``stt_engine`` is injected at registration time (Mirror's
post-voice-runtime block) — ``ToolContext`` does not carry the engine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)

if TYPE_CHECKING:
    from tesseract.voice.stt import STTEngine

logger = logging.getLogger(__name__)


class TranscribeAudioInput(BaseModel):
    attachment_id: str = Field(
        description=(
            "Attachment ID of an audio file the operator uploaded in this "
            "session. Audio is transcribed via the local Whisper engine "
            "(no per-call cost)."
        ),
        min_length=1,
    )


class TranscribeAudioTool(Tool):
    # Read-only, free, fast — no need to interrupt the operator.
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, stt_engine: "STTEngine | None" = None) -> None:
        self._stt_engine = stt_engine

    @property
    def name(self) -> str:
        return "transcribe_audio"

    @property
    def description(self) -> str:
        return (
            "Transcribe a previously-uploaded audio attachment using the "
            "local Whisper engine. Returns the transcript text. Note: "
            "audio attached to the operator's most recent message is "
            "auto-transcribed before you see it — only call this tool "
            "for ad-hoc re-transcription of older attachments."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return TranscribeAudioInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, TranscribeAudioInput)
            else TranscribeAudioInput(**tool_input.model_dump())
        )

        if self._stt_engine is None:
            return ToolResult(
                output=(
                    "audio_transcribe unavailable: STT engine not "
                    "initialized in this runtime"
                ),
                is_error=True,
            )

        # Resolve the attachment via the Mirror upload module. Late-imported
        # so the kernel/tools package stays loadable in REPL/test contexts.
        try:
            from tesseract.mirror.server.uploads import load_attachment
            from tesseract.mirror.server.uploads._storage import _attachment_file_path
        except ImportError:
            return ToolResult(
                output="audio_transcribe unavailable: Mirror upload module not loaded",
                is_error=True,
            )

        att = load_attachment(context.session_id, inp.attachment_id)
        if att is None:
            return ToolResult(
                output=f"attachment {inp.attachment_id!r} not found in this session",
                is_error=True,
            )
        if att.kind != "audio":
            return ToolResult(
                output=(
                    f"attachment {inp.attachment_id!r} is not audio "
                    f"(kind={att.kind!r}); use the appropriate reader instead"
                ),
                is_error=True,
            )

        file_path = _attachment_file_path(att)
        if file_path is None:
            return ToolResult(
                output=f"attachment {att.filename!r} not on disk",
                is_error=True,
            )

        if context is not None and context.status_emit is not None:
            try:
                await context.status_emit(
                    f"transcribing audio via local.whisper.local_whisper… ({att.filename})"
                )
            except Exception:  # noqa: BLE001
                logger.debug("transcribe_audio: status_emit failed", exc_info=True)

        try:
            audio_bytes = await asyncio.get_event_loop().run_in_executor(
                None, file_path.read_bytes,
            )
        except OSError as exc:
            return ToolResult(
                output=f"failed to read audio bytes: {exc}",
                is_error=True,
            )

        transcript = ""
        try:
            async for text, is_final in self._stt_engine.transcribe_stream(audio_bytes):
                if is_final:
                    transcript = (text or "").strip()
                    break
        except Exception as exc:  # noqa: BLE001
            logger.exception("transcribe_audio: STTEngine failed")
            return ToolResult(
                output=f"transcription failed: {exc}",
                is_error=True,
            )

        if not transcript:
            return ToolResult(
                output=f"[{att.filename}] (empty transcript)",
                metadata={"filename": att.filename, "attachment_id": att.id},
            )
        return ToolResult(
            output=transcript,
            metadata={
                "filename": att.filename,
                "attachment_id": att.id,
                "char_count": len(transcript),
            },
        )
