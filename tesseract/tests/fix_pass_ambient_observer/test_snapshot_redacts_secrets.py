"""MP-2 — view-snapshot privacy gate.

Backend defence-in-depth: the chat brain re-runs the same redaction
regex the frontend applies, so a misbehaving caller (test, integration,
external WS client) cannot leak token-shaped fields into the prompt.

Spec: any field name matching ``/(token|secret|password|api_?key|bot_?token)/i``
is replaced with the literal string ``[redacted]``. Walks nested dicts
and lists. Non-matching fields pass through untouched.
"""

from __future__ import annotations

from tesseract.brain.chat import _format_view_snapshot, _redact_view_snapshot


def test_redacts_top_level_token_and_secret_keys() -> None:
    state = {"token": "abc", "secret": "xyz", "open_sections": ["roles"]}
    out = _redact_view_snapshot(state)
    assert out["token"] == "[redacted]"
    assert out["secret"] == "[redacted]"
    assert out["open_sections"] == ["roles"]


def test_redacts_nested_keys() -> None:
    state = {
        "settings": {
            "telegram": {"bot_token": "secret-bot"},
            "openai": {"api_key": "sk-..."},
        },
        "panel": "channels",
    }
    out = _redact_view_snapshot(state)
    assert out["settings"]["telegram"]["bot_token"] == "[redacted]"
    assert out["settings"]["openai"]["api_key"] == "[redacted]"
    assert out["panel"] == "channels"


def test_redacts_within_lists() -> None:
    state = {"creds": [{"password": "p1"}, {"password": "p2"}]}
    out = _redact_view_snapshot(state)
    assert out["creds"][0]["password"] == "[redacted]"
    assert out["creds"][1]["password"] == "[redacted]"


def test_case_insensitive_match() -> None:
    state = {"API_KEY": "x", "BotToken": "y", "ApiToken": "z"}
    out = _redact_view_snapshot(state)
    assert out["API_KEY"] == "[redacted]"
    assert out["BotToken"] == "[redacted]"
    assert out["ApiToken"] == "[redacted]"


def test_format_view_snapshot_redacts_in_rendered_block() -> None:
    """End-to-end: the rendered block delivered to the prompt must
    carry `[redacted]`, never the raw secret."""
    snapshot = {
        "view": "settings",
        "view_state": {"telegram": {"bot_token": "REAL-SECRET-TOKEN"}},
    }
    block = _format_view_snapshot(snapshot)
    assert "REAL-SECRET-TOKEN" not in block
    assert "[redacted]" in block
    assert block.startswith("[current_view] settings")


def test_format_view_snapshot_handles_missing_view_state() -> None:
    """Non-dict ``view_state`` (or omitted) should not crash — the
    block renders with an empty JSON object."""
    block = _format_view_snapshot({"view": "soul"})
    assert "[current_view] soul" in block
    assert "[view_state] {}" in block


def test_format_view_snapshot_returns_empty_when_view_missing() -> None:
    assert _format_view_snapshot({}) == ""
    assert _format_view_snapshot({"view": ""}) == ""
    assert _format_view_snapshot({"view": None, "view_state": {"x": 1}}) == ""


def test_non_dict_values_pass_through() -> None:
    assert _redact_view_snapshot("plain") == "plain"
    assert _redact_view_snapshot(42) == 42
    assert _redact_view_snapshot(None) is None
    assert _redact_view_snapshot(True) is True
