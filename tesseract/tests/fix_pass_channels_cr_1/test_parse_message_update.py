"""CR-1: parse_message_update recognizes every Telegram update kind.

Each non-text kind round-trips to a :class:`ChannelAttachment` with
``status="no_handler"`` so the bridge can forward it through the
``<channel_attachment>`` envelope. The legacy "drop non-text" behavior
has been replaced — addressed-to-bot updates always yield a
:class:`TelegramMessage`.
"""

from __future__ import annotations

from tesseract.integrations._channel_attachment import ChannelAttachment
from tesseract.integrations.telegram.api import parse_message_update


def _base(**overrides):
    msg = {
        "message_id": 1,
        "date": 100,
        "chat": {"id": 99, "type": "private"},
        "from": {"id": 11, "username": "jane.doe"},
    }
    msg.update(overrides)
    return {"update_id": 42, "message": msg}


def test_text_only_still_parses_and_emits_no_attachments() -> None:
    out = parse_message_update(_base(text=" hello world "))
    assert out is not None
    assert out.text == "hello world"
    assert out.attachments == ()


def test_voice_attachment_no_handler() -> None:
    out = parse_message_update(
        _base(
            voice={
                "file_id": "AwACA",
                "duration": 12,
                "mime_type": "audio/ogg",
                "file_size": 4096,
            }
        )
    )
    assert out is not None
    assert out.text == ""
    assert len(out.attachments) == 1
    voice = out.attachments[0]
    assert voice.kind == "voice"
    assert voice.status == "no_handler"
    assert voice.source == "telegram"
    assert voice.mime == "audio/ogg"
    assert voice.duration_s == 12
    assert voice.size == 4096
    assert voice.ref == "AwACA"


def test_audio_attachment_no_handler() -> None:
    out = parse_message_update(
        _base(
            audio={
                "file_id": "AUDIO1",
                "duration": 90,
                "mime_type": "audio/mpeg",
                "file_size": 4_000_000,
                "file_name": "song.mp3",
            }
        )
    )
    assert out is not None
    audio = out.attachments[0]
    assert audio.kind == "audio"
    assert audio.filename == "song.mp3"
    assert audio.duration_s == 90


def test_photo_attachment_picks_highest_resolution() -> None:
    out = parse_message_update(
        _base(
            photo=[
                {"file_id": "lo", "width": 90, "height": 90, "file_size": 1000},
                {"file_id": "hi", "width": 1920, "height": 1080, "file_size": 200_000},
                {"file_id": "mid", "width": 800, "height": 600, "file_size": 30_000},
            ],
            caption="cat",
        )
    )
    assert out is not None
    photo = out.attachments[0]
    assert photo.kind == "photo"
    assert photo.ref == "hi"
    assert photo.width == 1920
    assert photo.height == 1080
    assert photo.caption == "cat"


def test_video_attachment_no_handler() -> None:
    out = parse_message_update(
        _base(
            video={
                "file_id": "VID",
                "duration": 30,
                "width": 1280,
                "height": 720,
                "mime_type": "video/mp4",
                "file_size": 5_000_000,
            }
        )
    )
    assert out is not None
    v = out.attachments[0]
    assert v.kind == "video"
    assert v.duration_s == 30
    assert v.width == 1280


def test_video_note_kind() -> None:
    out = parse_message_update(_base(video_note={"file_id": "VN", "duration": 5}))
    assert out is not None
    assert out.attachments[0].kind == "video_note"


def test_animation_kind() -> None:
    out = parse_message_update(_base(animation={"file_id": "AN", "duration": 3}))
    assert out is not None
    assert out.attachments[0].kind == "animation"


def test_document_attachment_no_handler() -> None:
    out = parse_message_update(
        _base(
            document={
                "file_id": "DOC",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 250_000,
            }
        )
    )
    assert out is not None
    doc = out.attachments[0]
    assert doc.kind == "document"
    assert doc.filename == "report.pdf"
    assert doc.mime == "application/pdf"
    assert doc.size == 250_000


def test_sticker_attachment_no_handler() -> None:
    out = parse_message_update(
        _base(sticker={"file_id": "STK", "emoji": "🙂", "set_name": "Cats", "width": 512, "height": 512})
    )
    assert out is not None
    sticker = out.attachments[0]
    assert sticker.kind == "sticker"
    assert "emoji=🙂" in (sticker.caption or "")


def test_sticker_with_user_caption_keeps_both_signals() -> None:
    """Regression for CR-1 review: the sticker factory pre-populates
    ``caption`` with machine descriptor (emoji + set_name); a
    user-supplied message ``caption`` must not clobber it."""
    out = parse_message_update(
        _base(
            sticker={"file_id": "STK", "emoji": "🙂", "set_name": "Cats"},
            caption="my reaction",
        )
    )
    assert out is not None
    caption = out.attachments[0].caption or ""
    assert "emoji=🙂" in caption
    assert "my reaction" in caption


def test_location_attachment_no_handler() -> None:
    out = parse_message_update(_base(location={"latitude": 52.5, "longitude": 13.4}))
    assert out is not None
    loc = out.attachments[0]
    assert loc.kind == "location"
    assert "lat=52.5" in (loc.caption or "")


def test_contact_attachment_no_handler() -> None:
    out = parse_message_update(
        _base(contact={"first_name": "John", "last_name": "Doe", "phone_number": "+1 555 0100"})
    )
    assert out is not None
    contact = out.attachments[0]
    assert contact.kind == "contact"
    assert "name=John Doe" in (contact.caption or "")


def test_poll_attachment_no_handler() -> None:
    out = parse_message_update(_base(poll={"question": "Which?"}))
    assert out is not None
    assert out.attachments[0].kind == "poll"
    assert out.attachments[0].caption == "Which?"


def test_dice_attachment_no_handler() -> None:
    out = parse_message_update(_base(dice={"emoji": "🎲", "value": 5}))
    assert out is not None
    dice = out.attachments[0]
    assert dice.kind == "dice"
    assert "value=5" in (dice.caption or "")


def test_text_plus_photo_carries_both_signals() -> None:
    out = parse_message_update(
        _base(
            text="look at this",
            photo=[{"file_id": "p1", "width": 100, "height": 100}],
            caption="ignored caption",
        )
    )
    assert out is not None
    assert out.text == "look at this"
    assert len(out.attachments) == 1
    photo: ChannelAttachment = out.attachments[0]
    assert photo.kind == "photo"
    # Caption stamps the first attachment so TARS sees text-with-photo
    # as a single unit rather than two unrelated bodies.
    assert photo.caption == "ignored caption"


def test_empty_addressed_message_yields_unknown_no_handler() -> None:
    """A message with no text and no recognized kind still surfaces —
    visibility-first means TARS sees ``something arrived``."""
    out = parse_message_update(_base())
    assert out is not None
    assert out.text == ""
    assert len(out.attachments) == 1
    assert out.attachments[0].kind == "unknown"
    assert out.attachments[0].status == "no_handler"


def test_structurally_broken_update_still_returns_none() -> None:
    assert parse_message_update({}) is None
    assert parse_message_update({"update_id": 1}) is None
    assert parse_message_update({"update_id": 1, "message": {}}) is None
