"""Test that session tools are registered in build_tool_registry."""

import pytest


def test_session_tools_registered(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    monkeypatch.setattr("tesseract.paths.TESSERACT_HOME", tmp_path)
    monkeypatch.setattr("tesseract.brain.boot.TESSERACT_HOME", tmp_path)

    from tesseract.brain.boot import build_tool_registry

    result = build_tool_registry(policy=None)
    registry = result[0]
    names = registry.names()
    for t in ("session_send", "session_result", "session_close", "session_list"):
        assert t in names, f"Tool '{t}' not found in registry; registered: {sorted(names)}"
    # session_open registers only when the chat_brain adapter resolves —
    # same guard as invoke_agent (`boot.py::build_tool_registry`), so its
    # presence must track that condition, not the environment.
    assert ("session_open" in names) == (registry.get("invoke_agent") is not None), (
        "session_open registration must match the chat-adapter condition"
    )
