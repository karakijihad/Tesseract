"""Shared helpers for subprocess-backed CLI adapters and delegate tools.

Extracted from `delegate_auditor.py` so `kernel/adapters/cli.py` (the
chat_brain adapter) and `kernel/tools/delegate_auditor.py` (the tool) both
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


class MCPTokenScopeError(RuntimeError):
    """The spawned process's hub identity could not be established."""


def scope_mcp_token(env: dict[str, str], client_name: str) -> dict[str, str]:
    """Leave the spawned CLI holding its own hub bearer token and no other.

    The backend's environment carries every configured client's token — the
    operator's included — and a child process inherits the lot. Ownership on
    the lane surface is keyed on the MCP client identity the bearer resolves
    to, so a process holding four tokens picks which principal to be and the
    owner check on the other end decides nothing.

    Deliberately NOT folded into the `*_subscription_env` builders above.
    Those serve every CLI-backed role (`kernel/adapters/cli.py::_build_env`),
    a scheduled job, and a non-lane delegate — none of which are lanes, and
    all of which are the runtime acting as the operator. Narrowing them would
    have demoted the assistant's own brain to a lane identity.

    Fails CLOSED. An unreadable `mcp.yaml` used to leave the environment
    untouched, which is the leak this function exists to close, arriving
    silently at the moment config is broken. A lane that cannot be given one
    identity must not be given all of them.
    """
    from tesseract.config.mcp import load_mcp_config

    try:
        clients = load_mcp_config().clients
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed
        raise MCPTokenScopeError(
            f"cannot scope the MCP token for {client_name!r}: mcp.yaml is "
            f"unreadable ({exc}). Refusing to spawn a process holding every "
            f"configured client's bearer token."
        ) from exc
    own = next((c.token_env for c in clients if c.name == client_name), None)
    if own is None:
        raise MCPTokenScopeError(
            f"mcp.yaml has no client named {client_name!r}; refusing to spawn "
            f"a process with an unresolvable hub identity"
        )
    for client in clients:
        if client.token_env != own:
            env.pop(client.token_env, None)
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
