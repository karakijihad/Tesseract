"""Tests for DelegateCodexExecTool — SU-3a headless slice.

All subprocess calls are mocked — no real codex binary is spawned.
Platform-agnostic: mock approach works identically on Windows and Linux.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tesseract.kernel.tools.base import PermissionResult, ToolContext
from tesseract.kernel.tools.delegate_codex_exec import (
    DelegateCodexExecInput,
    DelegateCodexExecTool,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


class FakeProc:
    """Minimal asyncio.Process stand-in."""

    def __init__(self, stdout: bytes, stderr: bytes, returncode: int, hang: bool = False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.kill = MagicMock()
        self._wait_called = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(10)  # will be cancelled by wait_for
        return self._stdout, self._stderr

    async def wait(self) -> int:
        self._wait_called = True
        return self.returncode


def _make_factory(proc: FakeProc):
    """Return an async callable matching create_subprocess_exec's signature."""
    async def _factory(*args, **kwargs):
        _factory.call_args = args
        _factory.call_kwargs = kwargs
        return proc

    _factory.call_args = ()
    _factory.call_kwargs = {}
    return _factory


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace_root=str(tmp_path), session_id="test-su3a")


@pytest.fixture()
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_call(monkeypatch, _home):
    proc = FakeProc(b"ok output\n", b"", returncode=0)
    factory = _make_factory(proc)
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.create_subprocess_exec",
        factory,
    )

    tool = DelegateCodexExecTool()
    result = await tool.run(DelegateCodexExecInput(prompt="review this"), _ctx(_home))

    assert result.is_error is False
    assert result.output == "ok output\n"


@pytest.mark.asyncio
async def test_nonzero_exit(monkeypatch, _home):
    proc = FakeProc(b"", b"error happened", returncode=1)
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.create_subprocess_exec",
        _make_factory(proc),
    )

    tool = DelegateCodexExecTool()
    result = await tool.run(DelegateCodexExecInput(prompt="audit this"), _ctx(_home))

    assert result.is_error is True
    assert "returned 1" in result.output
    assert "error happened" in result.output


@pytest.mark.asyncio
async def test_timeout(monkeypatch, _home):
    proc = FakeProc(b"", b"", returncode=0, hang=True)
    factory = _make_factory(proc)
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.create_subprocess_exec",
        factory,
    )

    # Patch wait_for so communicate() always times out, regardless of the
    # actual wall-clock duration. This avoids the 10s sleep in FakeProc AND
    # works within pydantic's timeout ge=5 constraint.
    _communicate_call_count = 0

    async def _wait_for_patch(coro, timeout):
        nonlocal _communicate_call_count
        _communicate_call_count += 1
        if _communicate_call_count == 1:
            # First call is proc.communicate() — simulate timeout.
            coro.close()
            raise asyncio.TimeoutError
        # Second call is proc.wait() after kill — return immediately.
        return await coro

    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.wait_for",
        _wait_for_patch,
    )

    tool = DelegateCodexExecTool()
    result = await tool.run(
        DelegateCodexExecInput(prompt="slow task", timeout=5), _ctx(_home)
    )

    assert result.is_error is True
    assert "timed out" in result.output
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_ansi_stripping(monkeypatch, _home):
    proc = FakeProc(b"\x1b[31mred\x1b[0m text", b"", returncode=0)
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.create_subprocess_exec",
        _make_factory(proc),
    )

    tool = DelegateCodexExecTool()
    result = await tool.run(DelegateCodexExecInput(prompt="show me colors"), _ctx(_home))

    assert result.is_error is False
    assert result.output == "red text"
    assert "\x1b" not in result.output


@pytest.mark.asyncio
async def test_codex_not_found(monkeypatch, _home):
    async def _raise(*args, **kwargs):
        raise FileNotFoundError("no such file: codex")

    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.create_subprocess_exec",
        _raise,
    )

    tool = DelegateCodexExecTool()
    result = await tool.run(DelegateCodexExecInput(prompt="anything"), _ctx(_home))

    assert result.is_error is True
    assert "codex executable not found" in result.output


def test_posture_is_auto(_home):
    tool = DelegateCodexExecTool()
    ctx = _ctx(_home)
    result = tool.check_permissions(DelegateCodexExecInput(prompt="x"), ctx)
    assert result == PermissionResult.PASSTHROUGH
    assert tool.default_posture == "auto"


def test_read_only_and_concurrent_safe(_home):
    tool = DelegateCodexExecTool()
    assert tool.is_read_only() is True
    assert tool.is_concurrency_safe() is True


def test_input_validation_empty_prompt():
    with pytest.raises(ValidationError):
        DelegateCodexExecInput(prompt="")


def test_input_validation_prompt_too_long():
    with pytest.raises(ValidationError):
        DelegateCodexExecInput(prompt="x" * 20001)


def test_input_validation_timeout_too_low():
    with pytest.raises(ValidationError):
        DelegateCodexExecInput(prompt="hello", timeout=4)


def test_input_validation_timeout_too_high():
    with pytest.raises(ValidationError):
        DelegateCodexExecInput(prompt="hello", timeout=1801)


@pytest.mark.asyncio
async def test_invocation_shape(monkeypatch, _home):
    """Verify the subprocess call is (executable, 'exec', prompt, ...)."""
    proc = FakeProc(b"result\n", b"", returncode=0)
    factory = _make_factory(proc)
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.create_subprocess_exec",
        factory,
    )
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.resolve_codex_executable",
        lambda: "codex",
    )

    tool = DelegateCodexExecTool()
    await tool.run(DelegateCodexExecInput(prompt="my prompt"), _ctx(_home))

    args = factory.call_args
    assert args[0] == "codex"
    assert args[1] == "exec"
    assert args[2] == "my prompt"


@pytest.mark.asyncio
async def test_env_strips_openai_api_key(monkeypatch, _home):
    """OPENAI_API_KEY must not appear in the env passed to the subprocess."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-should-be-stripped")

    proc = FakeProc(b"done\n", b"", returncode=0)
    factory = _make_factory(proc)
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_codex_exec.asyncio.create_subprocess_exec",
        factory,
    )

    tool = DelegateCodexExecTool()
    await tool.run(DelegateCodexExecInput(prompt="check env"), _ctx(_home))

    env_passed = factory.call_kwargs.get("env", {})
    assert "OPENAI_API_KEY" not in env_passed
