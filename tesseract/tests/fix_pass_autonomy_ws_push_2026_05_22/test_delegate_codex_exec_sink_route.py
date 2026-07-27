"""Phase 5 — delegate_codex_exec routes through the cli_sink when one is wired.

The headless `codex exec` path was invisible to the live spawn UI because
it used a bare `asyncio.create_subprocess_exec` + `communicate()`. With a
cli_sink + call_id on the ToolContext (chat-direct Mirror sessions), the
tool now routes through ``run_subprocess_with_sink`` which emits the
``cli_start`` / ``cli_output`` / ``cli_end`` envelopes the
``RunningSpawnsChip`` + delegate-transcript canvas card (D-6) consume.

Headless fallback (no sink, e.g. REPL / scheduler / mission) is preserved
byte-for-byte; the existing fix_pass_survivability_SU_3a suite covers it.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.delegate_codex_exec import (
    DelegateCodexExecInput,
    DelegateCodexExecTool,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


@pytest.mark.asyncio
async def test_sink_route_selected_when_cli_sink_and_call_id_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ToolContext.cli_sink + current_call_id are wired, delegate_codex_exec
    must route through run_subprocess_with_sink (which emits cli_* envelopes).
    """
    captured: dict[str, Any] = {}

    async def _fake_run_subprocess_with_sink(*, tool_name, argv, cwd, timeout, sink,
                                              call_id, empty_message, missing_message,
                                              env=None, **kwargs):
        captured["tool_name"] = tool_name
        captured["argv"] = tuple(argv)
        captured["call_id"] = call_id
        captured["sink_called"] = sink is not None
        # Emit a synthetic cli_start so the test confirms the sink is exercised.
        await sink("cli_start", call_id, {"tool": tool_name})
        await sink("cli_output", call_id, {"delta": "synthetic stdout"})
        await sink("cli_end", call_id, {"exit_code": 0})
        return ToolResult(output="synthetic stdout")

    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.run_subprocess_with_sink",
        _fake_run_subprocess_with_sink,
    )

    sink_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def _sink(kind: str, call_id: str, payload: dict[str, Any]) -> None:
        sink_calls.append((kind, call_id, payload))

    tool = DelegateCodexExecTool()
    ctx = ToolContext(
        session_id="sess-1",
        current_call_id="call-99",
        cli_sink=_sink,
    )
    result = await tool.run(DelegateCodexExecInput(prompt="audit the kernel"), ctx)

    assert not result.is_error
    assert result.output == "synthetic stdout"
    # Sink was exercised through the fake — proves we took the sink branch.
    assert captured["sink_called"] is True
    assert captured["tool_name"] == "delegate_codex_exec"
    assert captured["call_id"] == "call-99"
    assert "exec" in captured["argv"]
    assert "audit the kernel" in captured["argv"]
    kinds = [c[0] for c in sink_calls]
    assert kinds == ["cli_start", "cli_output", "cli_end"]


@pytest.mark.asyncio
async def test_headless_fallback_without_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no cli_sink is wired (REPL / scheduler), the bare-subprocess
    fallback runs and never calls run_subprocess_with_sink."""
    sink_called = {"flag": False}

    async def _fake_run_subprocess_with_sink(**kwargs):
        sink_called["flag"] = True
        return ToolResult(output="should never be reached")

    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.run_subprocess_with_sink",
        _fake_run_subprocess_with_sink,
    )

    # Replace the actual codex executable resolver with a no-op binary so the
    # bare-subprocess fallback path is exercised without depending on PATH.
    # `cmd /c echo OK` on Windows; `echo OK` on POSIX.
    import sys
    if sys.platform == "win32":
        fake_executable = "cmd"
    else:
        fake_executable = "echo"

    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.resolve_codex_executable",
        lambda: fake_executable,
    )

    tool = DelegateCodexExecTool()
    ctx = ToolContext(session_id="sess-1")  # no cli_sink, no call_id
    # Don't assert on the output (the fake `echo` won't match the codex
    # contract); just confirm the sink path was NOT taken.
    await tool.run(DelegateCodexExecInput(prompt="hello"), ctx)

    assert sink_called["flag"] is False, (
        "headless fallback path must not call run_subprocess_with_sink"
    )
