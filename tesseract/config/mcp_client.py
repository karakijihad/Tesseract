"""Outbound MCP client config (``mcp_servers.yaml``).

The curated allowlist of external MCP servers TARS may connect OUT to. This is
the inverse of ``config/mcp.py`` (which configures the inbound MCP *server*).

Config-as-authority (CLAUDE.md hard rule): every key is required — the loader
raises ``RuntimeError``/``KeyError`` on a missing or malformed value rather than
substituting a hardcoded infrastructure default (timeouts included). Mirrors the
raise-loudly convention of ``config/mcp.py``.

Security invariants (see ``Docs/Plan/capability-growth/_shared/security-contract.md``):
  1. Curation, not discovery — only servers listed here are ever contacted.
  2. ``tool_prefix`` required and UNIQUE across servers (namespacing stops an
     external tool shadowing a core tool name).
  3. No inline secrets — only ``auth_token_env`` / ``env_passthrough`` env-var
     NAMES, resolved from the process environment at connect time.
  4. ``transport`` is ``stdio`` (needs ``command``) or ``http`` (needs ``url``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tesseract.paths import CONFIG_DIR

MCP_SERVERS_YAML = CONFIG_DIR / "mcp_servers.yaml"

_VALID_TRANSPORTS = frozenset({"stdio", "http"})
_SECRET_KEYS = frozenset({"token", "secret", "api_key", "password", "auth_token"})


@dataclass(frozen=True)
class MCPServerSpec:
    name: str
    transport: str
    enabled: bool
    tool_prefix: str
    command: tuple[str, ...] = ()          # stdio only
    url: str | None = None                 # http only
    auth_token_env: str | None = None      # http only — env var NAME for bearer
    env_passthrough: tuple[str, ...] = ()  # env var NAMES forwarded to a stdio child


@dataclass(frozen=True)
class MCPClientDefaults:
    connect_timeout_s: int
    tool_call_timeout_s: int


@dataclass(frozen=True)
class MCPClientConfig:
    defaults: MCPClientDefaults
    servers: tuple[MCPServerSpec, ...]

    def enabled_servers(self) -> tuple[MCPServerSpec, ...]:
        return tuple(s for s in self.servers if s.enabled)


def load_mcp_client_config(path: Path = MCP_SERVERS_YAML) -> MCPClientConfig:
    """Load + validate ``mcp_servers.yaml``. Raises on any missing key or
    invariant violation — no silent defaults."""
    if not path.exists():
        raise FileNotFoundError(f"mcp client config missing: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"mcp_servers.yaml {path} did not parse to a mapping")

    defaults = _load_defaults(raw)
    servers = _load_servers(raw)
    return MCPClientConfig(defaults=defaults, servers=servers)


def _load_defaults(raw: dict[str, Any]) -> MCPClientDefaults:
    block = raw.get("defaults")
    if not isinstance(block, dict):
        raise RuntimeError("mcp_servers.yaml missing required 'defaults' block")
    try:
        connect_timeout_s = int(block["connect_timeout_s"])
        tool_call_timeout_s = int(block["tool_call_timeout_s"])
    except KeyError as exc:
        raise RuntimeError(f"mcp_servers.yaml defaults.* missing key: {exc.args[0]}") from exc
    if connect_timeout_s <= 0:
        raise RuntimeError("mcp_servers.yaml defaults.connect_timeout_s must be positive")
    if tool_call_timeout_s <= 0:
        raise RuntimeError("mcp_servers.yaml defaults.tool_call_timeout_s must be positive")
    return MCPClientDefaults(
        connect_timeout_s=connect_timeout_s,
        tool_call_timeout_s=tool_call_timeout_s,
    )


def _load_servers(raw: dict[str, Any]) -> tuple[MCPServerSpec, ...]:
    block = raw.get("servers")
    # A present-but-empty allowlist is a legitimate, explicit choice (no external
    # servers). A MISSING key is a config bug — fail loudly.
    if block is None or not isinstance(block, dict):
        raise RuntimeError("mcp_servers.yaml 'servers' must be a mapping (may be empty: {})")

    specs: list[MCPServerSpec] = []
    seen_prefixes: dict[str, str] = {}
    for name, entry in block.items():
        spec = _load_one_server(str(name), entry)
        # Invariant 2 — unique tool_prefix across servers.
        clash = seen_prefixes.get(spec.tool_prefix)
        if clash is not None:
            raise RuntimeError(
                f"mcp_servers.yaml servers.{name}.tool_prefix={spec.tool_prefix!r} "
                f"collides with server {clash!r}; prefixes must be unique"
            )
        seen_prefixes[spec.tool_prefix] = spec.name
        specs.append(spec)
    return tuple(specs)


def _load_one_server(name: str, entry: Any) -> MCPServerSpec:
    if not isinstance(entry, dict):
        raise RuntimeError(f"mcp_servers.yaml servers.{name} must be a mapping")

    # Invariant 3 — no inline secrets.
    inline = _SECRET_KEYS & entry.keys()
    if inline:
        raise RuntimeError(
            f"mcp_servers.yaml servers.{name} carries inline secret(s) {sorted(inline)}; "
            "use 'auth_token_env' / 'env_passthrough' (env-var NAMES) instead"
        )

    try:
        transport = str(entry["transport"]).strip().lower()
        enabled = bool(entry["enabled"])
        tool_prefix = str(entry["tool_prefix"])
    except KeyError as exc:
        raise RuntimeError(f"mcp_servers.yaml servers.{name}.* missing key: {exc.args[0]}") from exc

    if transport not in _VALID_TRANSPORTS:
        raise RuntimeError(
            f"mcp_servers.yaml servers.{name}.transport={transport!r} not in {sorted(_VALID_TRANSPORTS)}"
        )
    if not tool_prefix:
        raise RuntimeError(f"mcp_servers.yaml servers.{name}.tool_prefix must be non-empty")

    command: tuple[str, ...] = ()
    url: str | None = None
    auth_token_env: str | None = None

    if transport == "stdio":
        cmd_raw = entry.get("command")
        if not isinstance(cmd_raw, list) or not cmd_raw:
            raise RuntimeError(
                f"mcp_servers.yaml servers.{name}: stdio transport requires a non-empty 'command' list"
            )
        command = tuple(str(part) for part in cmd_raw)
    else:  # http
        url_raw = entry.get("url")
        if not isinstance(url_raw, str) or not url_raw.strip():
            raise RuntimeError(
                f"mcp_servers.yaml servers.{name}: http transport requires a non-empty 'url'"
            )
        url = url_raw.strip()
        auth_raw = entry.get("auth_token_env")
        if auth_raw is not None:
            auth_token_env = str(auth_raw)

    env_raw = entry.get("env_passthrough", [])
    if not isinstance(env_raw, list):
        raise RuntimeError(
            f"mcp_servers.yaml servers.{name}.env_passthrough must be a list of env-var names"
        )
    env_passthrough = tuple(str(v) for v in env_raw)

    return MCPServerSpec(
        name=name,
        transport=transport,
        enabled=enabled,
        tool_prefix=tool_prefix,
        command=command,
        url=url,
        auth_token_env=auth_token_env,
        env_passthrough=env_passthrough,
    )


__all__ = [
    "MCP_SERVERS_YAML",
    "MCPClientConfig",
    "MCPClientDefaults",
    "MCPServerSpec",
    "load_mcp_client_config",
]
