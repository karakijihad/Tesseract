"""Visibility-first envelope for channel inputs (CR-1).

Every channel adapter — Telegram today, WhatsApp/Signal/email/webhook
tomorrow — forwards every recognized input kind to the model, even when
no decoder is wired. The bridge concatenates a synthetic XML
``<channel_attachment>`` block onto the user's textual body before
passing it to ``_start_channel_turn``; the assistant sees what was sent and can
apologize, propose a tool, or file a workspace nudge.

"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Literal, Sequence

ChannelAttachmentKind = Literal[
    "voice",
    "audio",
    "photo",
    "video",
    "video_note",
    "animation",
    "document",
    "sticker",
    "location",
    "contact",
    "poll",
    "dice",
    "link",
    "unknown",
]

ChannelAttachmentStatus = Literal[
    "ready",
    "no_handler",
    "extract_failed",
    "too_large",
    "denied",
]

_KIND_ALLOWED: frozenset[str] = frozenset(
    {
        "voice",
        "audio",
        "photo",
        "video",
        "video_note",
        "animation",
        "document",
        "sticker",
        "location",
        "contact",
        "poll",
        "dice",
        "link",
        "unknown",
    }
)

_STATUS_ALLOWED: frozenset[str] = frozenset(
    {"ready", "no_handler", "extract_failed", "too_large", "denied"}
)


@dataclass(frozen=True)
class ChannelAttachment:
    """One non-text part of an inbound channel message.

    All optional fields use ``None`` to mean "unknown / not applicable"
    so the renderer can elide them without ambiguity. The ``ref`` field
    is an opaque adapter handle (e.g. Telegram ``file_id``) used by
    CR-2's handlers to re-fetch the bytes; CR-1 carries it forward but
    no decoder consumes it yet.
    """

    kind: ChannelAttachmentKind
    status: ChannelAttachmentStatus
    source: str
    mime: str | None = None
    size: int | None = None
    duration_s: int | None = None
    filename: str | None = None
    width: int | None = None
    height: int | None = None
    caption: str | None = None
    ref: str | None = None
    extracted: str | None = None
    error: str | None = None
    # Forward-slash path under uploads/channels (set after the bridge
    # persists the raw bytes via _channel_uploads.save_channel_attachment).
    # ``None`` until persisted; an extract-only attachment with no fetch
    # (e.g. sticker metadata) keeps ``None`` legitimately.
    storage_path: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KIND_ALLOWED:
            raise ValueError(f"ChannelAttachment.kind={self.kind!r} not in {_KIND_ALLOWED}")
        if self.status not in _STATUS_ALLOWED:
            raise ValueError(
                f"ChannelAttachment.status={self.status!r} not in {_STATUS_ALLOWED}"
            )
        if not self.source:
            raise ValueError("ChannelAttachment.source must be non-empty")


def render_envelope(attachments: Sequence[ChannelAttachment]) -> str:
    """Render ``attachments`` as one or more ``<channel_attachment>`` blocks.

    Empty input returns ``""``. Multiple attachments are separated by a
    single newline. Optional attributes whose value is ``None`` are
    elided. The bridge concatenates the result onto the user text with
    a blank-line separator (``{text}\\n\\n{envelope}``).
    """
    if not attachments:
        return ""
    blocks: list[str] = []
    for attachment in attachments:
        blocks.append(_render_one(attachment))
    return "\n".join(blocks)


def _render_one(attachment: ChannelAttachment) -> str:
    attrs: list[str] = [
        f'kind="{escape(attachment.kind, quote=True)}"',
        f'status="{escape(attachment.status, quote=True)}"',
        f'source="{escape(attachment.source, quote=True)}"',
    ]
    if attachment.mime is not None:
        attrs.append(f'mime="{escape(attachment.mime, quote=True)}"')
    if attachment.size is not None:
        attrs.append(f'size="{int(attachment.size)}"')
    if attachment.duration_s is not None:
        attrs.append(f'duration_s="{int(attachment.duration_s)}"')
    if attachment.filename is not None:
        attrs.append(f'filename="{escape(attachment.filename, quote=True)}"')
    if attachment.width is not None:
        attrs.append(f'width="{int(attachment.width)}"')
    if attachment.height is not None:
        attrs.append(f'height="{int(attachment.height)}"')
    if attachment.caption is not None:
        attrs.append(f'caption="{escape(attachment.caption, quote=True)}"')
    if attachment.ref is not None:
        attrs.append(f'ref="{escape(attachment.ref, quote=True)}"')
    if attachment.storage_path is not None:
        attrs.append(f'storage_path="{escape(attachment.storage_path, quote=True)}"')
    head = "<channel_attachment " + " ".join(attrs) + ">"
    body_parts: list[str] = []
    if attachment.status == "ready" and attachment.extracted:
        body_parts.append("<extracted>")
        body_parts.append(escape(attachment.extracted))
        body_parts.append("</extracted>")
    elif (
        attachment.status in ("extract_failed", "too_large", "denied")
        and attachment.error
    ):
        # Surface the *why* for non-ready statuses so the assistant can apologize
        # specifically ("voice was 700s, cap is 600s" beats "I couldn't
        # process that voice message"). Same body shape across the three
        # failure modes — the assistant reads them uniformly.
        body_parts.append("<extracted><error>")
        body_parts.append(escape(attachment.error))
        body_parts.append("</error></extracted>")
    if body_parts:
        return head + "\n" + "\n".join(body_parts) + "\n</channel_attachment>"
    return head + "</channel_attachment>"
