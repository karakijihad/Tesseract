"""Fixes for owner-notes observations #1 and #4.

- #1 — `ChatSession.prompt_builder` re-assembles the system prompt each
  turn so mid-session SOUL.md / IDENTITY.md edits land in the active
  session (previously frozen until next session).
- #4 — `_build_now_section` no longer reads `interaction_count` /
  `last_reflection` from SOUL.md frontmatter; nothing writes them, so
  the fields were dead weight.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

from tesseract.brain.chat import ChatSession
from tesseract.brain.prompt import _build_now_section, assemble_system_prompt
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk


class _CaptureAdapter(ModelAdapter):
    def __init__(self) -> None:
        self.last_system: str = ""

    async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
        return "unused"

    async def stream(
        self,
        messages,
        tools=None,
        options=None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.last_system = next(
            (m["content"] for m in messages if m.get("role") == "system"),
            "",
        )
        yield StreamChunk(type=ChunkType.TEXT, text="ok")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


# ── #1 — prompt_builder picks up mid-session edits ────────────────────────


async def test_prompt_builder_rebuilds_each_turn(tmp_path: Path) -> None:
    soul = tmp_path / "SOUL.md"
    soul.write_text("core truth one", encoding="utf-8")

    def _build() -> str:
        return soul.read_text(encoding="utf-8")

    adapter = _CaptureAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt="[frozen]",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        prompt_builder=_build,
        options=AdapterOptions(),
    )

    async for _ in session.send("turn 1"):
        pass
    assert adapter.last_system == "core truth one"

    soul.write_text("core truth two", encoding="utf-8")

    async for _ in session.send("turn 2"):
        pass
    assert adapter.last_system == "core truth two", \
        "mid-session SOUL edit must reach the active prompt"


async def test_prompt_builder_falls_back_to_frozen_on_error() -> None:
    def _bad_builder() -> str:
        raise RuntimeError("boom")

    adapter = _CaptureAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt="[frozen fallback]",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        prompt_builder=_bad_builder,
        options=AdapterOptions(),
    )

    async for _ in session.send("turn 1"):
        pass
    assert adapter.last_system == "[frozen fallback]"


async def test_prompt_builder_falls_back_to_frozen_on_empty() -> None:
    def _empty_builder() -> str:
        return ""

    adapter = _CaptureAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt="[frozen fallback]",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        prompt_builder=_empty_builder,
        options=AdapterOptions(),
    )

    async for _ in session.send("turn 1"):
        pass
    assert adapter.last_system == "[frozen fallback]"


async def test_no_prompt_builder_uses_frozen_string() -> None:
    adapter = _CaptureAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt="[frozen]",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
    )

    async for _ in session.send("hi"):
        pass
    assert adapter.last_system == "[frozen]"


# ── #4 — _build_now_section only emits Today ──────────────────────────────


def test_build_now_section_only_today() -> None:
    out = _build_now_section({"interaction_count": 42, "last_reflection": "2026-04-24T00:00:00Z"})
    assert "Today:" in out
    assert "Interactions recorded" not in out
    assert "Last reflection" not in out
    assert "none yet" not in out


def test_assemble_system_prompt_omits_dead_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    workspace.mkdir()
    store.mkdir()
    (workspace / "SOUL.md").write_text(
        "---\ninteraction_count: 99\nlast_reflection: 2026-04-24T00:00:00Z\n---\nbody\n",
        encoding="utf-8",
    )
    for name in ("IDENTITY.md", "USER.md", "AGENTS.md", "MCP.md"):
        (workspace / name).write_text("placeholder\n", encoding="utf-8")

    out = assemble_system_prompt(
        workspace_dir=workspace, memory_store_dir=store, mode="manifest"
    )
    assert "Interactions recorded" not in out
    assert "Last reflection" not in out
    assert "Today:" in out
