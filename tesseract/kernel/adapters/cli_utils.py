"""Shared helpers for subprocess-backed CLI adapters and delegate tools.

Extracted from `delegate_codex.py` so `kernel/adapters/cli.py` (the
chat_brain adapter) and `kernel/tools/delegate_codex.py` (the tool) both
import from one place. Underscore-prefixed names live here to avoid
adapters reaching into a tool's internals.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


def codex_subscription_env() -> dict[str, str]:
    """Force `codex` CLI onto ChatGPT subscription auth, never API key.

    Why: `codex exec` honours OPENAI_API_KEY ahead of the OAuth credential
    store. With the key set in the backend's env, calls silently burn API
    credits instead of using the operator's ChatGPT Plus plan.
    """
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    return env


def claude_subscription_env() -> dict[str, str]:
    """Force `claude` CLI onto OAuth subscription auth, never API key."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def resolve_codex_executable() -> str:
    """Return an executable path Windows CreateProcess can spawn.

    Prefer the native `codex.exe` vendored inside the npm package over the
    `codex.cmd` batch wrapper: a `.cmd` spawn routes argv through cmd.exe,
    which RE-PARSES `%*` — any newline or cmd metacharacter (`<`, `>`, `&`,
    quotes) in the task truncates/mangles the argv, silently dropping flags
    (trio W0 audit 2026-07-09, D1). The native binary receives argv intact.

    npm installs `codex` as a Unix-style script wrapper (no extension) plus
    `codex.cmd`/`codex.ps1`. asyncio's Windows subprocess transport does
    not run extensionless scripts; only `.cmd` (or `.exe`) is spawnable.
    """
    wrapper = shutil.which("codex.cmd")
    if wrapper:
        native = _native_codex_exe(Path(wrapper).parent)
        if native is not None:
            return str(native)
        # N6 — the codex.cmd wrapper re-parses argv through cmd.exe, which can
        # silently mangle a task containing newlines/metacharacters (W0 D1). It
        # stays the fallback because it is the only asyncio-spawnable option on
        # Windows when the native exe layout is absent — but surface the risk
        # loudly rather than resolving to it silently.
        log.warning(
            "resolve_codex_executable: native codex.exe not found under the npm "
            "package; falling back to the codex.cmd wrapper (%s). Tasks with "
            "newlines or cmd metacharacters (< > & quotes) may be corrupted — "
            "install/repair the vendored codex.exe to avoid this.",
            wrapper,
        )
        return wrapper
    return shutil.which("codex") or "codex"


def _native_codex_exe(npm_bin_dir: Path) -> Path | None:
    """Locate the platform-vendored `codex.exe` under the npm package that
    owns the `codex.cmd` wrapper. Returns None when the layout is not the
    known npm shape (fall back to the wrapper)."""
    pkg_root = npm_bin_dir / "node_modules" / "@openai" / "codex" / "node_modules"
    if not pkg_root.is_dir():
        return None
    for platform_pkg in pkg_root.glob("@openai/codex-*"):
        for exe in platform_pkg.glob("vendor/*/bin/codex.exe"):
            return exe
    return None
