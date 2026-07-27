"""Layer D — reset_with_reflection runs reflection before wiping.

Exercises the helper directly with a stub session so we don't need a
live adapter. The contract: history >= MIN_HISTORY_FOR_REFLECTION runs
reflect_on_session first, shorter sessions skip it; both paths wipe and
return a stable envelope.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tesseract.brain import session_ops
from tesseract.brain.session_ops import (
    MIN_HISTORY_FOR_REFLECTION,
    _summarize_reflection_call,
    reset_with_reflection,
)


# ── _summarize_reflection_call: field-name parity with the actual tools ──
# Each entry MUST stay in sync with the Pydantic input schema of the
# corresponding kernel tool — a typo here silently empties the workspace
# card snippet for that tool.

def _make_tc(name: str, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(name=name, input=kwargs)


def test_summarize_memory_save_extracts_title_and_content() -> None:
    tc = _make_tc("memory_save", title="op prefers terse", content="full body text")
    out = _summarize_reflection_call(tc)
    assert out == {
        "tool": "memory_save",
        "title": "op prefers terse",
        "snippet": "full body text",
        "status": "pending",
        "save_type": "",
    }


def test_summarize_diary_append_reads_text_field() -> None:
    """`DiaryAppendInput.text` is the canonical field — not `entry`."""
    tc = _make_tc("diary_append", text="felt clear today")
    out = _summarize_reflection_call(tc)
    assert out == {
        "tool": "diary_append",
        "title": "diary entry",
        "snippet": "felt clear today",
        "status": "pending",
        "save_type": "",
    }


def test_summarize_soul_growth_reads_bullet_field() -> None:
    tc = _make_tc("soul_growth_propose", bullet="pattern observed")
    out = _summarize_reflection_call(tc)
    assert out == {
        "tool": "soul_growth_propose",
        "title": "soul growth",
        "snippet": "pattern observed",
        "status": "pending",
        "save_type": "",
    }


def test_summarize_memory_save_falls_back_to_snippet_when_title_empty() -> None:
    """The user complaint that triggered this work: reflection cards
    showed `(no title)` because the model omitted `title`. Title now
    defaults to the first 80 chars of the content snippet so the card
    is never blank.
    """
    tc = _make_tc("memory_save", title="", content="The operator prefers async/await throughout, never callbacks.")
    out = _summarize_reflection_call(tc)
    assert out is not None
    assert out["title"] == "The operator prefers async/await throughout, never callbacks."


def test_summarize_skips_non_reflection_tools() -> None:
    tc = _make_tc("memory_search", query="anything")
    assert _summarize_reflection_call(tc) is None


def test_summarize_caps_snippet_at_200_chars() -> None:
    big = "x" * 1000
    tc = _make_tc("memory_save", title="t", content=big)
    out = _summarize_reflection_call(tc)
    assert out is not None
    assert len(out["snippet"]) == 200


def test_merge_result_metadata_attaches_destination_fields() -> None:
    """A `memory_save` TOOL_RESULT now carries the on-disk path and id in
    `raw["metadata"]` (see `kernel/tools/memory_save.py`). The reflection
    saves array surfaces that to the workspace card so the operator can
    see where the write landed.
    """
    from tesseract.brain.session_ops import _merge_result_metadata

    call: dict[str, Any] = {
        "tool": "memory_save",
        "title": "op prefers terse",
        "snippet": "...",
        "status": "pending",
    }
    chunk = SimpleNamespace(
        raw={
            "metadata": {
                "status": "saved",
                "memory_id": "mem_abc123",
                "path": "/repo/tesseract/memory-store/user/mem_abc123.md",
                "title": "op prefers terse",
            },
        },
        error="",
    )
    _merge_result_metadata(call, chunk)
    assert call["status"] == "saved"
    assert call["memory_id"] == "mem_abc123"
    assert call["path"].endswith("mem_abc123.md")


def test_merge_result_metadata_marks_blocked_on_error() -> None:
    """When the tool result chunk has no metadata but the tool errored,
    the saved-call summary must reflect that — otherwise the workspace
    card shows a happy "saved" state for a write that never happened.
    """
    from tesseract.brain.session_ops import _merge_result_metadata

    call: dict[str, Any] = {"tool": "memory_save", "status": "pending"}
    chunk = SimpleNamespace(raw={}, error="Memory blocked: type_mismatch")
    _merge_result_metadata(call, chunk)
    assert call["status"] == "blocked"


class _StubSession:
    def __init__(self, history: list[dict[str, Any]]) -> None:
        self.history = list(history)
        self.reset_called = False

    def reset(self) -> None:
        self.history.clear()
        self.reset_called = True


@pytest.mark.asyncio
async def test_reset_with_reflection_long_history_reflects_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = [{"role": "user", "content": "x"}] * MIN_HISTORY_FOR_REFLECTION
    session = _StubSession(history)
    seen: dict[str, Any] = {}

    fake_saves = [
        {"tool": "memory_save", "title": "t1", "snippet": "s1"},
        {"tool": "diary_append", "title": "diary entry", "snippet": "s2"},
        {"tool": "soul_growth_propose", "title": "soul growth", "snippet": "s3"},
    ]

    async def fake_reflect(s: Any, reason: str) -> list[dict[str, str]]:
        # Reflection sees the history *before* the wipe.
        seen["history_len_at_reflect"] = len(s.history)
        seen["reason"] = reason
        return fake_saves

    monkeypatch.setattr(session_ops, "reflect_on_session", fake_reflect)

    result = await reset_with_reflection(session, reason="ws_reset")  # type: ignore[arg-type]

    assert result["reflected"] is True
    assert result["saves"] == fake_saves
    assert seen["history_len_at_reflect"] == MIN_HISTORY_FOR_REFLECTION
    assert seen["reason"] == "ws_reset"
    assert session.reset_called is True
    assert session.history == []


@pytest.mark.asyncio
async def test_reset_with_reflection_short_history_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession([{"role": "user", "content": "x"}])
    called = False

    async def fake_reflect(s: Any, reason: str) -> list[dict[str, str]]:
        nonlocal called
        called = True
        return [{"tool": "memory_save", "title": "x", "snippet": "x"}]

    monkeypatch.setattr(session_ops, "reflect_on_session", fake_reflect)

    result = await reset_with_reflection(session)  # type: ignore[arg-type]

    assert called is False
    assert result["reflected"] is False
    assert result["saves"] == []
    assert session.reset_called is True


@pytest.mark.asyncio
async def test_reset_with_reflection_zero_history_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession([])

    async def fake_reflect(s: Any, reason: str) -> list[dict[str, str]]:
        raise AssertionError("reflect must not be called on empty session")

    monkeypatch.setattr(session_ops, "reflect_on_session", fake_reflect)

    result = await reset_with_reflection(session)  # type: ignore[arg-type]
    assert result["reflected"] is False
    assert result["saves"] == []


@pytest.mark.asyncio
async def test_reset_with_reflection_cancelled_still_wipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.CancelledError` during reflection must not leave the session
    in a half-state — the wipe is the operator's intent.
    """
    import asyncio

    history = [{"role": "user", "content": "x"}] * MIN_HISTORY_FOR_REFLECTION
    session = _StubSession(history)

    async def cancelled_reflect(s: Any, reason: str) -> list[dict[str, str]]:
        raise asyncio.CancelledError()

    monkeypatch.setattr(session_ops, "reflect_on_session", cancelled_reflect)

    result = await reset_with_reflection(session)  # type: ignore[arg-type]
    assert result["reflected"] is False
    assert result["saves"] == []
    assert session.reset_called is True
    assert session.history == []


@pytest.mark.asyncio
async def test_reset_with_reflection_returns_started_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession([{"role": "user", "content": "x"}])

    async def noop_reflect(s: Any, reason: str) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(session_ops, "reflect_on_session", noop_reflect)

    result = await reset_with_reflection(session)  # type: ignore[arg-type]
    assert "started_at" in result
    # ISO-8601 timestamp with timezone — same shape as do_reset's return.
    assert "T" in result["started_at"]
