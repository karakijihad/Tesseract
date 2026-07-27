"""CR-1: chunker — turn-aware for session JSON, heading-aware for
workshop markdown.

Pure functions; no SQLite, no FS. Tested in isolation so the indexer
can rely on stable chunk shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from tesseract.memory.work_ingester import (
    chunk_session_history,
    chunk_workshop_markdown,
)


def test_session_chunker_emits_one_per_user_assistant_turn() -> None:
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "what's the time"},
        {"role": "tool", "content": "tool result"},
        {"role": "assistant", "content": "it is 10am"},
    ]
    chunks = list(chunk_session_history(history))
    # Default: skip role=tool.
    assert all(c["role"] != "tool" for c in chunks)
    assert len(chunks) == 4
    assert chunks[0]["text"] == "hello"
    assert chunks[0]["turn_idx"] == 0
    assert chunks[1]["text"] == "hi"
    assert chunks[1]["turn_idx"] == 1


def test_session_chunker_can_include_tool_messages() -> None:
    history = [
        {"role": "user", "content": "do it"},
        {"role": "tool", "content": "tool out", "name": "bash"},
    ]
    chunks = list(chunk_session_history(history, include_tool=True))
    assert any(c["role"] == "tool" for c in chunks)


def test_session_chunker_handles_multimodal_content() -> None:
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "filename": "x.png"},
        ]},
    ]
    chunks = list(chunk_session_history(history))
    assert len(chunks) == 1
    assert "look at this" in chunks[0]["text"]


def test_session_chunker_skips_empty_content() -> None:
    history = [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "   "},
    ]
    chunks = list(chunk_session_history(history))
    assert chunks == []


def test_workshop_chunker_splits_by_heading() -> None:
    markdown = (
        "# Title\n\n"
        "Intro paragraph.\n\n"
        "## Section A\n\n"
        "Body of A.\n\n"
        "## Section B\n\n"
        "Body of B.\n"
    )
    chunks = list(chunk_workshop_markdown(markdown))
    assert len(chunks) >= 2
    # First chunk grabs the title + intro.
    assert "Intro paragraph" in chunks[0]["text"]
    # Section bodies preserved.
    flat = "\n---\n".join(c["text"] for c in chunks)
    assert "Body of A" in flat
    assert "Body of B" in flat


def test_workshop_chunker_handles_heading_only() -> None:
    chunks = list(chunk_workshop_markdown("# Just a title\n"))
    assert len(chunks) == 1
    assert "Just a title" in chunks[0]["text"]


def test_workshop_chunker_handles_no_headings() -> None:
    chunks = list(chunk_workshop_markdown("Plain text with no heading.\n"))
    assert len(chunks) == 1
    assert "Plain text" in chunks[0]["text"]
