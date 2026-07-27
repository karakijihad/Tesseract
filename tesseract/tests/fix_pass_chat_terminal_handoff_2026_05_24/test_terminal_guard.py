"""Terminal handoff guard — unit tests.

Verifies ``requires_terminal`` path logic and delegate tool refusal behaviour
for any target path under ``tesseract/mirror/**``.
"""

from __future__ import annotations

import pytest

from tesseract.kernel.tools._terminal_handoff_guard import (
    HANDOFF_REDIRECT_MESSAGE,
    requires_terminal,
)


# ---------------------------------------------------------------------------
# Guard function tests
# ---------------------------------------------------------------------------


def test_mirror_path_requires_terminal() -> None:
    assert requires_terminal(["tesseract/mirror/src/views/Chat.tsx"]) is True
    assert requires_terminal(["tesseract/mirror/server/app.py"]) is True


def test_non_mirror_paths_are_headless_ok() -> None:
    assert requires_terminal(["tesseract/brain/chat.py"]) is False
    assert requires_terminal([]) is False
    assert requires_terminal(None) is False


def test_mixed_paths_require_terminal_if_any_mirror() -> None:
    assert requires_terminal(["tesseract/brain/x.py", "tesseract/mirror/y.ts"]) is True


def test_windows_backslash_paths_match() -> None:
    assert requires_terminal(["tesseract\\\\mirror\\\\src\\\\App.tsx"]) is True


def test_redirect_message_names_the_handoff_tool() -> None:
    assert "start_controller_session" in HANDOFF_REDIRECT_MESSAGE
    assert "launch_terminal" in HANDOFF_REDIRECT_MESSAGE


# ---------------------------------------------------------------------------
# Delegate tool refusal tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_claude_refuses_mirror_path() -> None:
    """Guard fires before any subprocess; no real claude runs."""
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.delegate_claude import (
        DelegateClaudeInput,
        DelegateClaudeTool,
    )

    tool = DelegateClaudeTool()
    result = await tool.run(
        DelegateClaudeInput(
            task="update the chat component",
            target_paths=["tesseract/mirror/src/Chat.tsx"],
            background=False,
        ),
        ToolContext(),
    )

    assert result.is_error is True
    assert result.metadata is not None
    assert result.metadata["reason"] == "requires_terminal_handoff"
    assert "start_controller_session" in result.output


@pytest.mark.asyncio
async def test_delegate_codex_refuses_mirror_path() -> None:
    """Guard fires before any subprocess; no real codex runs."""
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.delegate_codex import (
        DelegateCodexInput,
        DelegateCodexTool,
    )

    tool = DelegateCodexTool()
    result = await tool.run(
        DelegateCodexInput(
            task="review the mirror server",
            target_paths=["tesseract/mirror/server/app.py"],
            background=False,
        ),
        ToolContext(),
    )

    assert result.is_error is True
    assert result.metadata is not None
    assert result.metadata["reason"] == "requires_terminal_handoff"
    assert "start_controller_session" in result.output


@pytest.mark.asyncio
async def test_delegate_claude_non_mirror_path_passes_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-mirror paths are NOT blocked by the guard (they may fail later for
    other reasons, e.g. CLI not found, but the guard does not intercept them)."""
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.delegate_claude import (
        DelegateClaudeInput,
        DelegateClaudeTool,
    )
    from tesseract.kernel.tools import delegate_claude as mod

    # Stub _cli_disabled_reason so the tool short-circuits cleanly after the
    # guard without attempting a real subprocess.
    monkeypatch.setattr(mod, "_cli_disabled_reason", lambda _p: "stubbed-for-test")

    tool = DelegateClaudeTool()
    result = await tool.run(
        DelegateClaudeInput(
            task="update the brain",
            target_paths=["tesseract/brain/chat.py"],
            background=False,
        ),
        ToolContext(),
    )

    # Guard did NOT fire — the stub returns a "disabled" error instead.
    assert result.metadata is None or result.metadata.get("reason") != "requires_terminal_handoff"
    assert "stubbed-for-test" in result.output
