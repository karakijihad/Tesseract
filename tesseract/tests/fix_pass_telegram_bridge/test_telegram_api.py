from __future__ import annotations

from tesseract.integrations.telegram.api import parse_message_update


def test_parse_message_update_reads_text_message() -> None:
    msg = parse_message_update(
        {
            "update_id": 42,
            "message": {
                "message_id": 7,
                "date": 123,
                "text": " hello ",
                "chat": {"id": 99, "type": "private"},
                "from": {"id": 11, "username": "jdoe"},
            },
        }
    )
    assert msg is not None
    assert msg.update_id == 42
    assert msg.chat_id == 99
    assert msg.message_id == 7
    assert msg.text == "hello"
    assert msg.from_username == "jdoe"


def test_parse_message_update_non_text_surfaces_unknown_attachment() -> None:
    """CR-1 lifted the visibility floor: an addressed-to-bot update with
    no recognized parts surfaces as ``kind="unknown" status="no_handler"``
    instead of being silently dropped."""
    msg = parse_message_update(
        {
            "update_id": 42,
            "message": {
                "message_id": 7,
                "date": 123,
                "chat": {"id": 99, "type": "private"},
            },
        }
    )
    assert msg is not None
    assert msg.text == ""
    assert len(msg.attachments) == 1
    assert msg.attachments[0].kind == "unknown"
    assert msg.attachments[0].status == "no_handler"
