"""MCP control-plane config (``mcp.yaml``).

Config-as-authority (CLAUDE.md hard rule): every key is required — the loader
raises ``RuntimeError``/``KeyError`` on a missing or malformed value rather
than substituting a hardcoded infrastructure default. Mirrors the raise-loudly
convention of ``config/cockpit.py``.

Schema + invariants: ``Docs/Plan/mcp-control-plane/_shared/mcp-yaml-schema.md``.

Posture is decided by this yaml alone. There is no source-side floor: the
operator's file is the single authority, which is why `config/mcp.yaml` is DENY
in `permissions.yaml` alongside `permissions.yaml` itself — it is a permissions
file, and the assistant must not be able to widen its own reach by editing it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from tesseract.paths import CONFIG_DIR

MCP_YAML = CONFIG_DIR / "mcp.yaml"

_LOCAL_BIND = "127.0.0.1"
_VALID_TRUST_TIERS = frozenset({"operator", "trusted", "restricted"})
_VALID_POSTURES = frozenset({"auto", "ask", "deny"})

# The verb surface from ``_shared/mcp-verb-surface.md``. This is a typo net
# only: a key absent from here is a config error (invariant 3), because a
# misspelled verb would sit in the yaml doing nothing. Posture is decided
# solely by ``mcp.yaml`` — there is no source-side floor, so the operator's
# yaml is the single authority. `config/mcp.yaml` is DENY in
# `permissions.yaml` for exactly that reason: it is now a permissions file.
_KNOWN_VERBS: frozenset[str] = frozenset(
    {
        "activity.list",
        "activity.watch",
        "activity.cancel",
        "memory.search",
        "vault.search",
        "vault.query",
        "memory.save",
        "memory.update",
        "vault.ingest",
        "lane.ensure",
        "lane.send",
        "lane.turn",
        "lane.read",
        "lane.close",
        "schedule.create",
        "schedule.update",
        "schedule.run",
        "schedule.remove",
        "schedule.list",
        "surface.open",
        "surface.spawn",
        "surface.update",
        "surface.focus",
        "surface.close",
        "budget.status",
        "budget.set_cap",
        "budget.pause_source",
        "agent.assign",
        "agent.status",
        "agent.review",
        "workspace.post",
        "workspace.reply",
        "workspace.read",
        "workspace.ask",
        "memory.recall",
        "memory.get",
        "memory.promote",
        "memory.forget",
        "diary.append",
        "feedback.propose",
    }
)

# auto < ask < deny — a posture may only move a verb UP this ladder.
_STRICTNESS = {"auto": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True)
class MCPClient:
    name: str
    token_env: str
    trust_tier: str


@dataclass(frozen=True)
class MCPServerBind:
    # host/port are the ADVERTISED endpoint (documentation + the loopback
    # invariant), NOT a bind — the MCP server is embedded in the Mirror app and
    # served on the Mirror socket. Only max_connections/ask_hold_timeout_s are
    # consumed at runtime.
    host: str
    port: int
    token_secret_env: str
    max_connections: int
    ask_hold_timeout_s: int
    idle_timeout_s: int


@dataclass(frozen=True)
class MCPStreamConfig:
    """Server→client push over the GET SSE stream (``activity.watch``).

    ``replay_buffer`` bounds what a reconnecting client can resume; a cursor
    older than the retained window is told it lost events rather than handed a
    partial history. ``client_queue`` bounds one slow consumer.

    ``max_streams_total``/``max_streams_per_session`` bound how MANY
    subscriptions may exist. ``server.max_connections`` gates session creation
    only, so without these one configured client could hold a single session
    open and stack unbounded streams — each with its own ``client_queue``-sized
    queue — behind it. Together the four keys are the whole memory ceiling of
    the subscription."""

    heartbeat_s: float
    replay_buffer: int
    client_queue: int
    max_streams_total: int
    max_streams_per_session: int


@dataclass(frozen=True)
class MCPConfig:
    server: MCPServerBind
    stream: MCPStreamConfig
    clients: tuple[MCPClient, ...]
    verbs: Mapping[str, str]  # verb -> posture, exactly as the operator wrote it
    trust_tiers: Mapping[str, str]  # trust tier -> posture cap (floor on strictness)

    def trust_tier_cap(self, tier: str) -> str:
        """Posture cap for a trust tier, layered into the effective posture via
        ``strictest(...)``. KeyError if the tier is unconfigured (the loader
        guarantees all three tiers are present)."""
        return self.trust_tiers[tier]


def strictest(*postures: str) -> str:
    """Return the strictest posture (highest on auto<ask<deny)."""
    return max(postures, key=lambda p: _STRICTNESS[p])


def load_mcp_config(path: Path = MCP_YAML) -> MCPConfig:
    """Load + validate ``mcp.yaml``. Raises on any missing key or invariant
    violation — no silent defaults."""
    if not path.exists():
        raise FileNotFoundError(f"mcp config missing: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"mcp config {path} did not parse to a mapping")

    server = _load_server(raw)
    stream = _load_stream(raw)
    clients = _load_clients(raw, server)
    verbs = _load_verbs(raw)
    trust_tiers = _load_trust_tiers(raw)
    return MCPConfig(
        server=server, stream=stream, clients=clients, verbs=verbs, trust_tiers=trust_tiers
    )


def _positive_number(block: dict[str, Any], key: str, *, integer: bool) -> Any:
    """One required, positive, finite ``stream.*`` value.

    Bare ``int()``/``float()`` accepted more than the schema means: ``True``
    coerces to ``1``, ``"8"`` to ``8``, ``1.9`` silently truncates to ``1``,
    and — the one that actually bypassed a check — ``.nan`` compares False
    against every ``<= 0`` test and sailed through as a valid heartbeat.
    """
    try:
        value = block[key]
    except KeyError as exc:
        raise RuntimeError(f"mcp.yaml stream.* missing key: {key}") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"mcp.yaml stream.{key} must be a number, got {value!r}")
    if integer and not isinstance(value, int):
        raise RuntimeError(f"mcp.yaml stream.{key} must be a whole number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"mcp.yaml stream.{key} must be positive")
    return value


def _load_stream(raw: dict[str, Any]) -> MCPStreamConfig:
    block = raw.get("stream")
    if not isinstance(block, dict):
        raise RuntimeError("mcp.yaml missing required 'stream' block")
    heartbeat_s = float(_positive_number(block, "heartbeat_s", integer=False))
    replay_buffer = _positive_number(block, "replay_buffer", integer=True)
    client_queue = _positive_number(block, "client_queue", integer=True)
    max_streams_total = _positive_number(block, "max_streams_total", integer=True)
    max_streams_per_session = _positive_number(block, "max_streams_per_session", integer=True)
    if max_streams_per_session > max_streams_total:
        raise RuntimeError(
            "mcp.yaml stream.max_streams_per_session must not exceed max_streams_total "
            f"({max_streams_per_session} > {max_streams_total})"
        )
    return MCPStreamConfig(
        heartbeat_s=heartbeat_s,
        replay_buffer=replay_buffer,
        client_queue=client_queue,
        max_streams_total=max_streams_total,
        max_streams_per_session=max_streams_per_session,
    )


def _load_trust_tiers(raw: dict[str, Any]) -> Mapping[str, str]:
    tiers = raw.get("trust_tiers")
    if not isinstance(tiers, dict):
        raise RuntimeError("mcp.yaml 'trust_tiers' must be a mapping (tier -> cap posture)")
    out: dict[str, str] = {}
    for tier, posture_raw in tiers.items():
        if tier not in _VALID_TRUST_TIERS:
            raise RuntimeError(
                f"mcp.yaml trust_tiers.{tier} not in {sorted(_VALID_TRUST_TIERS)}"
            )
        posture = str(posture_raw).strip().lower()
        if posture not in _VALID_POSTURES:
            raise RuntimeError(
                f"mcp.yaml trust_tiers.{tier} posture {posture!r} not in {sorted(_VALID_POSTURES)}"
            )
        out[tier] = posture
    # Every tier a client may hold must have a cap — no silent default.
    missing = _VALID_TRUST_TIERS - out.keys()
    if missing:
        raise RuntimeError(f"mcp.yaml trust_tiers missing required tier(s): {sorted(missing)}")
    return out


def _load_server(raw: dict[str, Any]) -> MCPServerBind:
    block = raw.get("server")
    if not isinstance(block, dict):
        raise RuntimeError("mcp.yaml missing required 'server' block")
    try:
        host = str(block["host"])
        port = int(block["port"])
        token_secret_env = str(block["token_secret_env"])
        max_connections = int(block["max_connections"])
        ask_hold_timeout_s = int(block["ask_hold_timeout_s"])
        idle_timeout_s = int(block["idle_timeout_s"])
    except KeyError as exc:
        raise RuntimeError(f"mcp.yaml server.* missing key: {exc.args[0]}") from exc
    # Invariant 1 — advertised host is loopback-locked. MCP is embedded in the
    # Mirror app (local-only); this asserts the documented endpoint can't claim
    # an externally-routable address.
    if host != _LOCAL_BIND:
        raise RuntimeError(
            f"mcp.yaml server.host must be {_LOCAL_BIND!r} (local hive only); got {host!r}"
        )
    if port <= 0:
        raise RuntimeError("mcp.yaml server.port must be positive")
    if not token_secret_env:
        raise RuntimeError("mcp.yaml server.token_secret_env must be a non-empty env var name")
    if max_connections <= 0:
        raise RuntimeError("mcp.yaml server.max_connections must be positive")
    if ask_hold_timeout_s <= 0:
        raise RuntimeError("mcp.yaml server.ask_hold_timeout_s must be positive")
    if idle_timeout_s <= 0:
        raise RuntimeError("mcp.yaml server.idle_timeout_s must be positive")
    return MCPServerBind(
        host=host,
        port=port,
        token_secret_env=token_secret_env,
        max_connections=max_connections,
        ask_hold_timeout_s=ask_hold_timeout_s,
        idle_timeout_s=idle_timeout_s,
    )


def _load_clients(raw: dict[str, Any], server: MCPServerBind) -> tuple[MCPClient, ...]:
    entries = raw.get("clients")
    # Invariant 2 — at least one client (default-deny needs a known identity).
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("mcp.yaml 'clients' must be a non-empty list")
    clients: list[MCPClient] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"mcp.yaml clients[{i}] must be a mapping")
        # Invariant 5 — no inline secrets; only *_env indirections.
        if "token" in entry or "secret" in entry:
            raise RuntimeError(
                f"mcp.yaml clients[{i}] carries an inline secret; use 'token_env' instead"
            )
        try:
            name = str(entry["name"])
            token_env = str(entry["token_env"])
            trust_tier = str(entry["trust_tier"])
        except KeyError as exc:
            raise RuntimeError(f"mcp.yaml clients[{i}].* missing key: {exc.args[0]}") from exc
        if trust_tier not in _VALID_TRUST_TIERS:
            raise RuntimeError(
                f"mcp.yaml clients[{i}].trust_tier={trust_tier!r} not in {sorted(_VALID_TRUST_TIERS)}"
            )
        clients.append(MCPClient(name=name, token_env=token_env, trust_tier=trust_tier))

    # Invariant 6 — exactly one operator client, whose token_env ties back to
    # server.token_secret_env (so the primary secret names a defined client).
    operators = [c for c in clients if c.trust_tier == "operator"]
    if len(operators) != 1:
        raise RuntimeError(
            f"mcp.yaml must define exactly one 'operator'-tier client (got {len(operators)})"
        )
    if operators[0].token_env != server.token_secret_env:
        raise RuntimeError(
            "mcp.yaml server.token_secret_env must equal the operator client's token_env "
            f"({server.token_secret_env!r} != {operators[0].token_env!r})"
        )
    return tuple(clients)


def _load_verbs(raw: dict[str, Any]) -> Mapping[str, str]:
    verbs = raw.get("verbs")
    # Invariant — default-deny allowlist must exist (empty means nothing works,
    # which is a legitimate but explicit choice; a missing key is a config bug).
    if not isinstance(verbs, dict):
        raise RuntimeError("mcp.yaml 'verbs' must be a mapping (default-deny allowlist)")
    out: dict[str, str] = {}
    for verb, posture_raw in verbs.items():
        posture = str(posture_raw).strip().lower()
        if posture not in _VALID_POSTURES:
            raise RuntimeError(f"mcp.yaml verbs.{verb} posture {posture!r} not in {sorted(_VALID_POSTURES)}")
        # Invariant 3 — every allowlisted verb must be a known surface verb.
        # This is a typo net, not a posture rule: a misspelled key would
        # otherwise sit in the yaml doing nothing while the operator believed
        # it was in force.
        if verb not in _KNOWN_VERBS:
            raise RuntimeError(
                f"mcp.yaml verbs.{verb} is not a known verb (see mcp-verb-surface.md)"
            )
        out[verb] = posture
    return out


__all__ = [
    "MCPClient",
    "MCPServerBind",
    "MCPStreamConfig",
    "MCPConfig",
    "load_mcp_config",
    "strictest",
    "MCP_YAML",
]
