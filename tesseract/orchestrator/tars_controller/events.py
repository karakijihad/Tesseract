"""Typed transcript event models.

Contract: `Docs/Plan/tars-terminal-controller/_shared/transcript-events.md`.
Required fields on every event: `event_id`, `session_id`, `ts`, `kind`,
`origin`. Kind-specific payload follows.

Unknown / extension kinds (e.g. anything a future phase adds before its
renderer lands) round-trip through `GenericTranscriptEvent` without data
loss — extra fields are preserved via Pydantic's `extra="allow"` config.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _mint_event_id() -> str:
    return f"evt-{secrets.token_hex(8)}"


Origin = Literal["chat", "cli", "autonomy", "scheduler", "telegram", "mirror"]


class BaseTranscriptEvent(BaseModel):
    """Shared envelope. Subclasses pin `kind` and add payload."""

    model_config = ConfigDict(extra="allow")

    event_id: str = Field(default_factory=_mint_event_id)
    session_id: str
    ts: str = Field(default_factory=_now_iso)
    kind: str
    origin: str


class UserTextEvent(BaseTranscriptEvent):
    kind: Literal["user_text"] = "user_text"
    text: str
    actor_id: str = "operator"


class AssistantTextEvent(BaseTranscriptEvent):
    kind: Literal["assistant_text"] = "assistant_text"
    text: str
    partial: bool = False
    model_role: str = "chat_brain"
    worker_id: str | None = None


class ToolUseEvent(BaseTranscriptEvent):
    kind: Literal["tool_use"] = "tool_use"
    tool: str
    input: dict[str, Any] = Field(default_factory=dict)
    tool_use_id: str
    worker_id: str | None = None


class ToolResultEvent(BaseTranscriptEvent):
    kind: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    success: bool
    output: dict[str, Any] | str | None = None
    timed_out: bool = False


class PermissionRequestEvent(BaseTranscriptEvent):
    kind: Literal["permission_request"] = "permission_request"
    tool: str
    summary: str
    posture: Literal["auto", "ask", "deny"]
    resolved: bool = False
    resolution: str | None = None


class WorkerStatusEvent(BaseTranscriptEvent):
    kind: Literal["worker_status"] = "worker_status"
    worker_id: str
    worker_kind: str
    status: str
    progress: str | None = None


class ArtifactEvent(BaseTranscriptEvent):
    kind: Literal["artifact"] = "artifact"
    worker_id: str | None = None
    artifact_type: Literal["file", "patch", "summary"]
    path: str | None = None
    content_summary: str | None = None


class ChildTranscriptRefEvent(BaseTranscriptEvent):
    kind: Literal["child_transcript_ref"] = "child_transcript_ref"
    child_session_id: str
    child_transcript_path: str
    worker_id: str | None = None


class JournalEntryEvent(BaseTranscriptEvent):
    kind: Literal["journal_entry"] = "journal_entry"
    entry_type: Literal["approval", "dispatch", "outcome", "advice_only"]
    agenda_item_id: str | None = None
    worker_id: str | None = None
    summary: str | None = None


class PtyChunkEvent(BaseTranscriptEvent):
    kind: Literal["pty_chunk"] = "pty_chunk"
    worker_id: str
    data_b64: str
    encoding: Literal["base64"] = "base64"


class CliChunkEvent(BaseTranscriptEvent):
    """Streaming subprocess output from ``delegate_claude`` / ``delegate_codex``
    and other ``CliSink``-driven tools. The chat brain's tool-execution
    layer calls ``ToolContext.cli_sink(kind, call_id, payload)`` with
    chunked stdout; the controller wraps each chunk in this event so the
    TUI renders it inline under the parent tool_use line — mirrors
    Claude CLI's "see what the sub-process is doing live" affordance.

    ``stream`` lets the renderer distinguish stdout from stderr when the
    chat brain plumbs both (currently combined to stdout via
    ``stderr=STDOUT`` in ``run_subprocess_with_sink``, but the field
    leaves room for future stderr separation).
    """

    kind: Literal["cli_chunk"] = "cli_chunk"
    tool: str
    tool_use_id: str
    text: str
    stream: Literal["stdout", "stderr"] = "stdout"
    phase: Literal["start", "chunk", "end"] = "chunk"
    exit_code: int | None = None


class SessionMetricsEvent(BaseTranscriptEvent):
    """Per-turn model + usage + context snapshot for the statusline.

    Emitted by the controller dispatch loop at turn start, on every
    ``ChunkType.STOP`` (which carries adapter-side usage), on
    ``ChunkType.MODEL_SELECTED`` (model swap inside a fallback chain),
    and at turn end. The renderer keeps the most recent payload and
    paints it into the dock-bottom ``StatusBar``.

    Fields are optional so partial updates ride the same envelope —
    e.g. a MODEL_SELECTED-driven event only carries ``model`` /
    ``provider`` / ``role``, while a STOP-driven event adds tokens +
    context.
    """

    kind: Literal["session_metrics"] = "session_metrics"
    model: str | None = None
    provider: str | None = None
    role: str | None = None
    tier: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    context_window: int | None = None
    context_used: int | None = None
    cost_usd: float | None = None
    turn_state: Literal["idle", "thinking", "tool", "streaming", "done", "error"] = "idle"


class GenericTranscriptEvent(BaseTranscriptEvent):
    """Forward-compatible fallback for unknown / future event kinds.

    Used by `parse_event` when no specific model matches the row's `kind`.
    Extra fields are preserved by the `extra="allow"` config inherited
    from `BaseTranscriptEvent`, so a write→read round-trip is lossless.
    """


TranscriptEvent = Union[
    UserTextEvent,
    AssistantTextEvent,
    ToolUseEvent,
    ToolResultEvent,
    PermissionRequestEvent,
    WorkerStatusEvent,
    ArtifactEvent,
    ChildTranscriptRefEvent,
    JournalEntryEvent,
    PtyChunkEvent,
    CliChunkEvent,
    SessionMetricsEvent,
    GenericTranscriptEvent,
]


_TYPED_KINDS: dict[str, type[BaseTranscriptEvent]] = {
    "user_text": UserTextEvent,
    "assistant_text": AssistantTextEvent,
    "tool_use": ToolUseEvent,
    "tool_result": ToolResultEvent,
    "permission_request": PermissionRequestEvent,
    "worker_status": WorkerStatusEvent,
    "artifact": ArtifactEvent,
    "child_transcript_ref": ChildTranscriptRefEvent,
    "journal_entry": JournalEntryEvent,
    "pty_chunk": PtyChunkEvent,
    "cli_chunk": CliChunkEvent,
    "session_metrics": SessionMetricsEvent,
}


def parse_event(payload: dict[str, Any]) -> TranscriptEvent:
    """Dispatch a JSON dict to its specific event model.

    Unknown kinds yield `GenericTranscriptEvent` with all fields preserved.
    """
    kind = payload.get("kind")
    model = _TYPED_KINDS.get(str(kind)) if isinstance(kind, str) else None
    if model is None:
        return GenericTranscriptEvent.model_validate(payload)
    return model.model_validate(payload)


__all__ = [
    "ArtifactEvent",
    "AssistantTextEvent",
    "BaseTranscriptEvent",
    "ChildTranscriptRefEvent",
    "CliChunkEvent",
    "GenericTranscriptEvent",
    "JournalEntryEvent",
    "Origin",
    "PermissionRequestEvent",
    "PtyChunkEvent",
    "SessionMetricsEvent",
    "ToolResultEvent",
    "ToolUseEvent",
    "TranscriptEvent",
    "UserTextEvent",
    "WorkerStatusEvent",
    "parse_event",
]
