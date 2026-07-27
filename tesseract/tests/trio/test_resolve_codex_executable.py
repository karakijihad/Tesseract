"""W1 D1 — `resolve_codex_executable` must prefer the native vendored
`codex.exe` over the npm `codex.cmd` batch wrapper (cmd.exe `%*` re-parsing
mangles newlines/metachars in the task argv — W0 audit D1)."""

from __future__ import annotations

import shutil

from tesseract.kernel.adapters import cli_utils


def _npm_layout(tmp_path, with_native: bool):
    """Build the npm global layout: <bin>/codex.cmd + the vendored exe."""
    bin_dir = tmp_path / "npm"
    bin_dir.mkdir()
    wrapper = bin_dir / "codex.cmd"
    wrapper.write_text("@echo off\n", encoding="utf-8")
    if with_native:
        exe = (
            bin_dir / "node_modules" / "@openai" / "codex" / "node_modules"
            / "@openai" / "codex-win32-x64" / "vendor"
            / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
        )
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        return wrapper, exe
    return wrapper, None


def test_native_exe_preferred_over_cmd_wrapper(tmp_path, monkeypatch):
    wrapper, exe = _npm_layout(tmp_path, with_native=True)
    monkeypatch.setattr(
        shutil, "which",
        lambda name: str(wrapper) if name == "codex.cmd" else None,
    )
    assert cli_utils.resolve_codex_executable() == str(exe)


def test_falls_back_to_cmd_wrapper_without_native(tmp_path, monkeypatch):
    wrapper, _ = _npm_layout(tmp_path, with_native=False)
    monkeypatch.setattr(
        shutil, "which",
        lambda name: str(wrapper) if name == "codex.cmd" else None,
    )
    assert cli_utils.resolve_codex_executable() == str(wrapper)


def test_falls_back_to_plain_codex_without_wrapper(monkeypatch):
    monkeypatch.setattr(
        shutil, "which",
        lambda name: "/usr/local/bin/codex" if name == "codex" else None,
    )
    assert cli_utils.resolve_codex_executable() == "/usr/local/bin/codex"


def test_bare_name_when_nothing_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert cli_utils.resolve_codex_executable() == "codex"
