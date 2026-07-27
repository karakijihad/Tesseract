"""CR-3 — channel prompt overlay renders inside the assembled manifest.

Pure-addition contract from
``Docs/Plan/channels-redesign/phase-CR-3-channel-prompt-overlay.md``:

- ``assemble_system_prompt(channel_name=None)`` (cockpit) is byte-identical
  to pre-CR-3 — no overlay anywhere in the rendered prompt.
- ``assemble_system_prompt(channel_name="Telegram")`` (channel) inlines
  the overlay *before* the per-turn "Right now" section so it rides the
  cacheable manifest prefix.
- ``mirror/server/session.py::_build_chat_session`` plumbs a
  channel-aware ``prompt_builder`` when the bridge passes
  ``kind="channel"``; the resulting ``ChatSession`` sends a system
  message that contains the overlay every turn.

The end-to-end ``ChatSession.send`` check uses a fake adapter that
captures the system message — no live LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.brain.prompt import (
    CHANNEL_OVERLAY_HEADER,
    assemble_system_prompt,
    build_channel_overlay,
)
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


# --- assemble_system_prompt: overlay placement ----------------------------


def _seed_workspace(workspace: Path) -> None:
    workspace.mkdir()
    for name in ("IDENTITY.md", "USER.md", "AGENTS.md", "MCP.md", "SOUL.md"):
        (workspace / name).write_text("placeholder\n", encoding="utf-8")


def test_cockpit_assembly_omits_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    _seed_workspace(workspace)
    store.mkdir()
    out = assemble_system_prompt(
        workspace_dir=workspace, memory_store_dir=store, mode="manifest"
    )
    assert CHANNEL_OVERLAY_HEADER not in out


def test_channel_assembly_inlines_overlay_before_right_now(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    _seed_workspace(workspace)
    store.mkdir()
    out = assemble_system_prompt(
        workspace_dir=workspace,
        memory_store_dir=store,
        mode="manifest",
        channel_name="Telegram",
    )
    assert CHANNEL_OVERLAY_HEADER in out
    assert "(Telegram)" in out
    overlay_idx = out.index(CHANNEL_OVERLAY_HEADER)
    right_now_idx = out.index("# Right now")
    assert overlay_idx < right_now_idx, (
        "overlay must precede '# Right now' so it stays inside the cacheable "
        "manifest prefix (CR-3 phase doc §2 manifest-mode caching)"
    )


def test_channel_assembly_overlay_substitutes_channel_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    _seed_workspace(workspace)
    store.mkdir()
    out = assemble_system_prompt(
        workspace_dir=workspace,
        memory_store_dir=store,
        mode="manifest",
        channel_name="Signal",
    )
    assert "(Signal)" in out
    assert "(Telegram)" not in out


def test_channel_assembly_is_pure_addition(tmp_path: Path) -> None:
    """Channel manifest must contain the cockpit manifest as a prefix up
    to the channel overlay — overlay is ADDED, never replaces a section."""
    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    _seed_workspace(workspace)
    store.mkdir()
    cockpit = assemble_system_prompt(
        workspace_dir=workspace, memory_store_dir=store, mode="manifest"
    )
    channel = assemble_system_prompt(
        workspace_dir=workspace,
        memory_store_dir=store,
        mode="manifest",
        channel_name="Telegram",
    )
    # The "Right now" section is generated each call but uses today's date,
    # so within the same test process both renders produce the same line.
    # Strip everything from "# Right now" onward and compare prefixes.
    sentinel = "# Right now"
    assert sentinel in cockpit, f"sentinel '{sentinel}' missing — _build_now_section renamed?"
    assert sentinel in channel, f"sentinel '{sentinel}' missing — _build_now_section renamed?"
    cockpit_prefix = cockpit.split(sentinel, 1)[0]
    channel_prefix = channel.split(sentinel, 1)[0]
    assert channel_prefix.startswith(cockpit_prefix), (
        "channel manifest must be cockpit manifest + overlay; cockpit prefix "
        "differs which means CR-3 mutated the base prompt"
    )
    overlay_only = channel_prefix[len(cockpit_prefix):]
    assert CHANNEL_OVERLAY_HEADER in overlay_only


# --- build_channel_overlay: unit ------------------------------------------


def test_build_channel_overlay_substitutes_name() -> None:
    out = build_channel_overlay("Signal")
    assert "(Signal)" in out
    assert out.startswith(CHANNEL_OVERLAY_HEADER)


def test_build_channel_overlay_falls_back_on_missing_name() -> None:
    out_none = build_channel_overlay(None)
    out_empty = build_channel_overlay("   ")
    assert "a remote messaging channel" in out_none
    assert out_none == out_empty


def test_overlay_size_is_bounded() -> None:
    """Phase doc §4 estimates "~600 chars" but the §2 instruction block
    renders ~1650 chars verbatim, and the 2026-05-17 outbound-tools +
    memory hint block adds ~700 more. Cap raised to 3000 to accommodate
    the new sections — the new guidance is real value (it fixed the
    "TARS pastes /api/downloads/ URL as text" bug) but we still want to
    catch accidental bloat past the new ceiling."""
    out = build_channel_overlay("Telegram")
    assert len(out) < 3000, f"overlay grew to {len(out)} chars — re-tighten §2"


# --- end-to-end ChatSession turn ------------------------------------------


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


@pytest.mark.asyncio
async def test_channel_chat_session_sends_overlay_in_system_prompt(
    tmp_path: Path,
) -> None:
    """``ChatSession.send`` for a channel-built session puts the overlay
    on the wire as part of the ``role:system`` message."""
    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    _seed_workspace(workspace)
    store.mkdir()

    def _channel_prompt_builder() -> str:
        return assemble_system_prompt(
            workspace_dir=workspace,
            memory_store_dir=store,
            mode="manifest",
            channel_name="Telegram",
        )

    adapter = _CaptureAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt=_channel_prompt_builder(),
        max_tool_iterations=1,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
        prompt_builder=_channel_prompt_builder,
        session_kind="channel",
        channel_display_name="Telegram",
    )
    async for _ in session.send("hi"):
        pass
    assert CHANNEL_OVERLAY_HEADER in adapter.last_system
    assert "(Telegram)" in adapter.last_system


@pytest.mark.asyncio
async def test_cockpit_chat_session_does_not_send_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = tmp_path / "memory-store"
    _seed_workspace(workspace)
    store.mkdir()

    def _cockpit_prompt_builder() -> str:
        return assemble_system_prompt(
            workspace_dir=workspace, memory_store_dir=store, mode="manifest"
        )

    adapter = _CaptureAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt=_cockpit_prompt_builder(),
        max_tool_iterations=1,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(),
        prompt_builder=_cockpit_prompt_builder,
    )
    async for _ in session.send("hi"):
        pass
    assert CHANNEL_OVERLAY_HEADER not in adapter.last_system
