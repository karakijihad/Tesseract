"""Auto-provisions MCP hub connectivity into a lane's or terminal's working
directory so a spawned/hand-launched ``claude``/``codex`` CLI wakes up
already connected to the embedded hub (``tesseract/config/mcp.yaml``).

Claude Code: merges a project-scope ``.mcp.json`` — ``mcpServers.tesseract``
entry, HTTP transport, env-expanded ``Authorization`` header. Shape per
code.claude.com/docs/en/mcp ("Environment variable expansion" — ``${VAR}``
syntax; ``.mcp.json`` project scope).

Codex: merges the user-global ``~/.codex/config.toml`` ``[mcp_servers.
tesseract]`` table — ``url`` + ``bearer_token_env_var`` (native env
indirection, no literal secret). Shape per
developers.openai.com/codex/config-reference. Codex config is global, not
per-project, so every codex-capable provision call targets the same file.

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

_MCP_JSON_NAME = ".mcp.json"
_CODEX_BLOCK_HEADER = "[mcp_servers.tesseract]"
# ``~/.codex/config.toml`` is a single global file every codex/terminal
# provision call reads-modifies-writes — serialize access so two concurrent
# callers (e.g. a codex lane opening alongside a terminal pane, "parallel by
# default") can't interleave a torn read/write.
_CODEX_CONFIG_LOCK = threading.Lock()

# M12 — the project ``.mcp.json`` is read-modify-written by lane, delegate, and
# terminal provisioning, which run in parallel (asyncio.to_thread) against the
# same working dir. Serialize per resolved path so concurrent writers can't
# tear the file or race the TOCTOU identity-precedence snapshot.
_MCP_JSON_LOCKS: dict[str, threading.Lock] = {}
_MCP_JSON_LOCKS_GUARD = threading.Lock()


def _mcp_json_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _MCP_JSON_LOCKS_GUARD:
        lock = _MCP_JSON_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _MCP_JSON_LOCKS[key] = lock
        return lock


def provision(working_dir: Path, kind: ProvisionKind, mcp_cfg: MCPConfig) -> None:
    """Ensure the hub is reachable from ``working_dir`` (claude) and/or the
    operator's codex config (codex). ``kind="terminal"`` provisions both,
    since a hand-launched terminal may run either CLI.

    Identity precedence (trio W1, Doclog 2026-07-09): a terminal provision
    never overwrites an existing LANE identity — lane and terminal write the
    same ``.mcp.json`` key / global codex table, so last-writer-wins used to
    re-identify every co-located lane as ``terminal-manual`` (W0 audit D4).
    Lane provisions still overwrite freely (lane wins)."""
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
        _provision_claude_mcp_json(working_dir, url, client.token_env, preserve_envs)
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


def _provision_claude_mcp_json(
    working_dir: Path, url: str, token_env: str, preserve_envs: frozenset[str] = frozenset()
) -> None:
    path = working_dir / _MCP_JSON_NAME
    # Hold the per-path lock across the whole read-modify-write so the identity
    # snapshot and the write are atomic w.r.t. concurrent provisions (M12).
    with _mcp_json_lock(path):
        working_dir.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if path.exists():
            text = path.read_text(encoding="utf-8")
            existing = json.loads(text) if text.strip() else {}
            if not isinstance(existing, dict):
                raise RuntimeError(f"mcp_provision: {path} did not parse to a JSON object")
        servers = existing.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise RuntimeError(f"mcp_provision: {path} 'mcpServers' is not an object")
        if _auth_header_env(servers.get("tesseract")) in preserve_envs:
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
