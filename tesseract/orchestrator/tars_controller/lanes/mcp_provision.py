"""Auto-provisions MCP hub connectivity for ``claude``/``codex`` so a spawned
or hand-launched CLI wakes up already connected to the embedded hub
(``tesseract/config/mcp.yaml``).

Both CLIs are provisioned at **user scope** — one file each, applying in every
directory. Project scope was the original choice for Claude Code, and it tied
connectivity to the directory the pane happened to open in: a lane or an
operator who moved somewhere else before launching got a CLI with no hub at
all. What actually distinguishes "launched inside TESSERACT" from any other
shell is the bearer token in the environment, which only the runtime's own
child processes inherit — so directory scope bought nothing and cost reach.

Claude Code: merges ``~/.claude.json`` ``mcpServers.tesseract`` — HTTP
transport, env-expanded ``Authorization`` header (``${VAR}`` syntax).
Codex: merges ``~/.codex/config.toml`` ``[mcp_servers.tesseract]`` — ``url`` +
``bearer_token_env_var`` (native env indirection, no literal secret).

Neither file is ours. ``~/.claude.json`` in particular holds a live session's
own state and is rewritten by it constantly, so both writers no-op when the
entry already matches: provisioning touches disk when the endpoint or identity
actually changes, and never again.

Config-as-authority: raises if the resolved client's token env var is unset
at provision time — a dead hub connection is caught at spawn, not first use.
Idempotent; never touches unrelated keys in either file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Literal

from tesseract.config.mcp import MCPConfig

from .models import LaneKind

ProvisionKind = LaneKind | Literal["terminal"]

_CLIENT_NAME_BY_KIND: dict[str, str] = {
    "claude": "lane-claude",
    "codex": "lane-codex",
    "terminal": "terminal-manual",
}

_CLAUDE_CONFIG_NAME = ".claude.json"
_CODEX_BLOCK_HEADER = "[mcp_servers.tesseract]"
# Each CLI's config is a single global file that every lane, delegate, and
# terminal provision call reads-modifies-writes, and those run in parallel
# (asyncio.to_thread). One lock per file so concurrent callers can't interleave
# a torn read/write or race the identity-precedence snapshot.
_CODEX_CONFIG_LOCK = threading.Lock()
_CLAUDE_CONFIG_LOCK = threading.Lock()


def provision(kind: ProvisionKind, mcp_cfg: MCPConfig) -> None:
    """Ensure the hub is reachable from the operator's claude and/or codex
    config. ``kind="terminal"`` provisions both, since a hand-launched
    terminal may run either CLI.

    Identity precedence: a terminal provision never overwrites an existing
    LANE identity. Both CLIs' configs are global, so lane and terminal write
    the same key, and last-writer-wins would re-identify every live lane as
    ``terminal-manual``. Lane provisions still overwrite freely (lane wins).

    The flip side of global scope is that two claude identities cannot be held
    at once — a lane and a manual terminal share one entry. Codex has always
    worked this way; claude now matches."""
    client_name = _CLIENT_NAME_BY_KIND.get(kind)
    if client_name is None:
        raise RuntimeError(f"mcp_provision: unknown kind {kind!r}")
    client = next((c for c in mcp_cfg.clients if c.name == client_name), None)
    if client is None:
        raise RuntimeError(
            f"mcp_provision: mcp.yaml has no client named {client_name!r} (kind={kind!r})"
        )
    if not os.environ.get(client.token_env):
        raise RuntimeError(
            f"mcp_provision: required env var {client.token_env!r} for client "
            f"{client_name!r} is unset — refusing to provision a dead hub connection"
        )

    url = f"http://{mcp_cfg.server.host}:{mcp_cfg.server.port}/mcp"
    preserve_envs: frozenset[str] = frozenset()
    if kind == "terminal":
        preserve_envs = frozenset(
            c.token_env
            for c in mcp_cfg.clients
            if c.name in _CLIENT_NAME_BY_KIND.values() and c.name != client_name
        )

    if kind in ("claude", "terminal"):
        _provision_claude_user_config(url, client.token_env, preserve_envs)
    if kind in ("codex", "terminal"):
        _provision_codex_config_toml(url, client.token_env, preserve_envs)


def _auth_header_env(entry: object) -> str | None:
    """Extract the ``${VAR}`` name from an ``.mcp.json`` server entry's
    Authorization header, or None when the shape doesn't match."""
    if not isinstance(entry, dict):
        return None
    headers = entry.get("headers")
    if not isinstance(headers, dict):
        return None
    match = re.fullmatch(r"Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(headers.get("Authorization") or ""))
    return match.group(1) if match else None


def _provision_claude_user_config(
    url: str, token_env: str, preserve_envs: frozenset[str] = frozenset()
) -> None:
    # Hold the lock across the whole read-modify-write so the identity snapshot
    # and the write are atomic w.r.t. concurrent provisions.
    with _CLAUDE_CONFIG_LOCK:
        path = Path.home() / _CLAUDE_CONFIG_NAME
        existing: dict = {}
        if path.exists():
            text = path.read_text(encoding="utf-8")
            existing = json.loads(text) if text.strip() else {}
            if not isinstance(existing, dict):
                raise RuntimeError(f"mcp_provision: {path} did not parse to a JSON object")
        servers = existing.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise RuntimeError(f"mcp_provision: {path} 'mcpServers' is not an object")
        current = servers.get("tesseract")
        current_env = _auth_header_env(current)
        if isinstance(current, dict) and current.get("url") == url and current_env == token_env:
            return  # already provisioned by this exact client — no-op
        if current_env in preserve_envs:
            return  # existing lane identity wins over a terminal provision
        servers["tesseract"] = {
            "type": "http",
            "url": url,
            "headers": {"Authorization": f"Bearer ${{{token_env}}}"},
        }
        _atomic_write(path, json.dumps(existing, indent=2) + "\n")


def _provision_codex_config_toml(
    url: str, token_env: str, preserve_envs: frozenset[str] = frozenset()
) -> None:
    with _CODEX_CONFIG_LOCK:
        codex_dir = Path.home() / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        path = codex_dir / "config.toml"
        existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
        parsed = tomllib.loads(existing_text) if existing_text.strip() else {}
        section = parsed.get("mcp_servers")
        current = section.get("tesseract") if isinstance(section, dict) else None
        if (
            isinstance(current, dict)
            and current.get("url") == url
            and current.get("bearer_token_env_var") == token_env
        ):
            return  # already provisioned by this exact client — no-op
        if (
            isinstance(current, dict)
            and current.get("bearer_token_env_var") in preserve_envs
        ):
            # Existing lane identity wins over a terminal provision — the
            # codex config is GLOBAL, so one pane open must not re-identify
            # every future codex lane turn (W0 audit D4).
            return

        block = (
            f"{_CODEX_BLOCK_HEADER}\n"
            f'url = "{url}"\n'
            f'bearer_token_env_var = "{token_env}"\n'
        )
        lines = existing_text.splitlines(keepends=True)
        start = next(
            (i for i, ln in enumerate(lines) if ln.strip() == _CODEX_BLOCK_HEADER), None
        )
        if start is not None:
            end = start + 1
            while end < len(lines) and not lines[end].lstrip().startswith("["):
                end += 1
            new_text = "".join(lines[:start]) + block + "".join(lines[end:])
        elif existing_text.strip():
            new_text = existing_text.rstrip("\n") + "\n\n" + block
        else:
            new_text = block
        _atomic_write(path, new_text)


def _atomic_write(path: Path, content: str) -> None:
    # M12 — a unique temp per write (was a single shared ``<path>.tmp``) so two
    # concurrent writers to the same target can't collide on the temp file
    # (WinError 32) or clobber each other's staging. Same-dir temp keeps the
    # os.replace atomic. Callers serialize per path; this is defense in depth.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # Windows can transiently deny the replace when an indexer/AV briefly
        # holds the just-created target; retry within a bounded budget before
        # giving up (only kicks in under contention — the normal path replaces
        # once with no sleep).
        for attempt in range(20):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.02)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = ["provision", "ProvisionKind"]
