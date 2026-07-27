"""CR-1: render_envelope emits the shared ``<channel_attachment>`` shape."""

from __future__ import annotations

from tesseract.integrations._channel_attachment import (
    ChannelAttachment,
    render_envelope,
)


def test_empty_returns_empty_string() -> None:
    assert render_envelope([]) == ""


def test_voice_no_handler_block() -> None:
    out = render_envelope(
        [
            ChannelAttachment(
                kind="voice",
                status="no_handler",
                source="telegram",
                mime="audio/ogg",
                size=4096,
                duration_s=12,
                ref="AwACA",
            )
        ]
    )
    assert out == (
        '<channel_attachment kind="voice" status="no_handler" source="telegram" '
        'mime="audio/ogg" size="4096" duration_s="12" ref="AwACA"></channel_attachment>'
    )


def test_photo_with_caption() -> None:
    out = render_envelope(
        [
            ChannelAttachment(
                kind="photo",
                status="no_handler",
                source="telegram",
                size=20000,
                width=1920,
                height=1080,
                caption="cat on a hat",
                ref="hi",
            )
        ]
    )
    assert "kind=\"photo\"" in out
    assert "caption=\"cat on a hat\"" in out
    assert "width=\"1920\"" in out
    assert "ref=\"hi\"" in out


def test_two_attachments_separated_by_newline() -> None:
    out = render_envelope(
        [
            ChannelAttachment(kind="voice", status="no_handler", source="telegram"),
            ChannelAttachment(kind="document", status="no_handler", source="telegram"),
        ]
    )
    blocks = out.split("\n")
    assert len(blocks) == 2
    assert blocks[0].startswith("<channel_attachment kind=\"voice\"")
    assert blocks[1].startswith("<channel_attachment kind=\"document\"")


def test_unknown_kind_no_handler() -> None:
    out = render_envelope(
        [ChannelAttachment(kind="unknown", status="no_handler", source="telegram")]
    )
    assert out == (
        '<channel_attachment kind="unknown" status="no_handler" source="telegram">'
        "</channel_attachment>"
    )


def test_ready_status_emits_extracted_block() -> None:
    out = render_envelope(
        [
            ChannelAttachment(
                kind="voice",
                status="ready",
                source="telegram",
                duration_s=12,
                extracted="hello world",
            )
        ]
    )
    assert "<extracted>\nhello world\n</extracted>" in out


def test_extract_failed_emits_error_block() -> None:
    out = render_envelope(
        [
            ChannelAttachment(
                kind="document",
                status="extract_failed",
                source="telegram",
                error="pdf parser threw",
            )
        ]
    )
    assert "<extracted><error>\npdf parser threw\n</error></extracted>" in out


def test_special_chars_in_caption_are_escaped() -> None:
    out = render_envelope(
        [
            ChannelAttachment(
                kind="poll",
                status="no_handler",
                source="telegram",
                caption='He said "<bold>hi</bold>" & ran',
            )
        ]
    )
    assert "&quot;" in out
    assert "&lt;bold&gt;" in out
    assert "&amp;" in out
