"""Shared helpers for subprocess-backed CLI adapters and delegate tools.

Extracted from `delegate_auditor.py` so `kernel/adapters/cli.py` (the
chat_brain adapter) and `kernel/tools/delegate_auditor.py` (the tool) both
import from one place. Underscore-prefixed names live here to avoid
adapters reaching into a tool's internals.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


REAPABLE_ENV = "TESSERACT_REAPABLE"


def mark_reapable(env: dict[str, str], owner: str) -> dict[str, str]:
    """Stamp a spawned CLI's environment with who launched it.

    The janitor reaps orphans, and until this existed the only handle it had
    on one was a regex over the command line — which cannot tell a lane the
    runtime started from the operator's own `claude -p` in a terminal, and
    which knows nothing at all about a CLI nobody wrote a pattern for.
    `psutil.Process().environ()` reads this back, so identity replaces the
    guess: marked means ours, unmarked-and-readable means leave it alone.

    Every CLI child inherits it, which is intended — an orphaned MCP server
    or tool process a lane left behind is that lane's leftover. It is
    deliberately NOT stamped on the runtime's own `-m tesseract.*` spawns:
    the backend would pass it to every subprocess it runs on the operator's
    behalf (a bash command, git, ollama), and `janitor.yaml`'s
    `tesseract-runtime` pattern already names those exactly and can never
    match a foreign process.

    The name carries no credential, so `probe_env`'s scrub leaves it.
    """
    env[REAPABLE_ENV] = owner
    return env


def codex_subscription_env() -> dict[str, str]:
    """Force `codex` CLI onto ChatGPT subscription auth, never API key.

    Why: `codex exec` honours OPENAI_API_KEY ahead of the OAuth credential
    store. With the key set in the backend's env, calls silently burn API
    credits instead of using the operator's ChatGPT Plus plan.
    """
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    return mark_reapable(env, "codex")


def claude_subscription_env() -> dict[str, str]:
    """Force `claude` CLI onto OAuth subscription auth, never API key."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return mark_reapable(env, "claude")


def subscription_env(command: str) -> dict[str, str]:
    """The environment a CLI-tier provider is spawned with, by command name.

    One lookup for every caller — the chat adapter, the delegate tools, the
    nightly live check — so a provider added to the catalog cannot end up
    spending API credit from one call site and the subscription from another.

    The reapable mark rides the same lookup, for the same reason: a CLI
    provider added to the catalog is spawned through here, so it is marked
    the day it arrives rather than the day someone remembers to write it a
    janitor pattern.
    """
    if command == "codex":
        return codex_subscription_env()
    if command == "claude":
        return claude_subscription_env()
    return mark_reapable(os.environ.copy(), command)


def probe_env(command: str) -> dict[str, str]:
    """The environment a health probe spawns its CLI with: no credential in it.

    A probe is not the assistant working. It asks one trivial question, and
    `codex exec` — which is what the live check runs — is an agentic turn, not
    a status command. Handing it the backend's whole environment gives a
    trojaned or PATH-hijacked binary every API key and every MCP bearer token
    on the machine, on a schedule, with no operator prompt in the way.
    `--sandbox read-only` bounds the filesystem; it bounds nothing here.

    **Config names the secrets, and a pattern catches the rest.** Every
    `api_key_env` in `providers.yaml` and every `token_env` in `mcp.yaml` is
    the authoritative list of what this runtime keeps in the environment, so
    that is the first cut. The name pattern is the backstop for anything
    config does not know about — a key someone put in `.env` by hand, a
    variable a library reads directly.

    **Deliberately not an allowlist**, though one was tried first and is the
    stronger control on paper. Measured: `claude auth status` spawned with an
    OS-only allowlist exits `0xC0000409`, a hard crash, because these CLIs read
    more of the environment than can be enumerated from outside them. A
    control that stops the check running is not a control.

    Never raises. Unreadable config falls back to the pattern alone, which
    still matches the `*_API_KEY` and `*_TOKEN` shapes those entries have.

    **What the pattern was tested against is two CLIs on one machine.** A
    provider added later whose CLI reads, say, a credential-store path with
    `KEY` in the name would lose it and the probe would report `ours`. That
    fails in the safe direction, but it reports the wrong thing — so a new
    `cli` provider is worth one manual run of its probe.

    Not `subscription_env`, which serves the assistant's real calls and must
    carry what those calls need. Two callers, two questions. `command` selects
    that provider's own subscription scrub on top.
    """
    env = subscription_env(command)
    # Resolved ONCE. Inside the loop this re-read and re-parsed two YAML files
    # for every environment variable that did not match the pattern — about
    # seventy of them per spawn, on the event loop that carries WS heartbeats
    # and inbound turns.
    configured = _configured_secret_env()
    for name in list(env):
        if _SECRET_NAME.search(name) or name in configured:
            env.pop(name, None)
    return mark_reapable(env, f"probe-{command}")


# Anything whose NAME says it is a credential. Broad on purpose: a false
# positive costs a probe one variable it did not need, a false negative hands
# a spawned binary a key.
_SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?AUTH|BEARER", re.IGNORECASE
)


def _configured_secret_env() -> frozenset[str]:
    """Every environment variable this runtime's config names as a credential.

    Read fresh rather than cached: the config watcher rebuilds adapters
    without a restart, so a key added to `providers.yaml` at 14:00 must be
    scrubbed from the 15:15 probe.
    """
    names: set[str] = set()
    try:
        from tesseract.config.loader import load_config

        for _ref, conn, _model in load_config().all_models():
            if conn.api_key_env:
                names.add(conn.api_key_env)
    except Exception:  # noqa: BLE001 — the pattern above is the backstop
        log.warning("probe_env: providers.yaml unreadable; scrubbing by name only")
    try:
        from tesseract.config.mcp import load_mcp_config

        names.update(c.token_env for c in load_mcp_config().clients if c.token_env)
    except Exception:  # noqa: BLE001
        log.warning("probe_env: mcp.yaml unreadable; scrubbing by name only")
    return frozenset(names)


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

    Fails CLOSED. Leaving the environment untouched on an unreadable
    `mcp.yaml` is the leak this function exists to close, and it would arrive
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


def resolve_cli_executable(name: str) -> str:
    """Return something CreateProcess can actually spawn for a bare CLI name.

    `asyncio.create_subprocess_exec("codex", ...)` raises `FileNotFoundError`
    on Windows: npm installs the CLI as an extensionless shell script plus a
    `codex.cmd` shim, and the Windows subprocess transport does not consult
    PATHEXT. The auth probe raised that every night it ran and filed its own
    launch failure as the provider's state.

    `shutil.which` does consult PATHEXT, so it is enough for any CLI that
    ships a `.exe` or a `.cmd`. `codex` is named here rather than handled
    generically because its npm package vendors a native binary whose
    location only the codex layout knows --- see `resolve_codex_executable`
    for why that binary is preferred over the `.cmd` shim.

    Falls back to `name` unchanged so the caller's own error path reports a
    missing binary, rather than this returning something that is not there.
    """
    if name == "codex":
        return resolve_codex_executable()
    return shutil.which(name) or name


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
