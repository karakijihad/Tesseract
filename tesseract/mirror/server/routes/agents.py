"""Agents registry REST routes — read-only until MO-8 (Provisional Registry)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from tesseract.agents.loader import (
    AgentDefinition,
    list_agents,
    list_pending_agents,
    load_agent,
    read_agent_source,
    resolve_agent_path,
    save_agent_source,
    set_agent_disabled,
)

log = logging.getLogger(__name__)


def _load_bundle() -> Any | None:
    """The config bundle, or None if it cannot be read.

    Loaded ONCE per request and threaded through the serializers. It used to be
    loaded per agent inside `_resolve_model_ref`, which meant a full YAML parse
    of the config tree for every row — with twenty agents, twenty parses on the
    event loop, and `/api/agents` taking eight seconds while every other
    request queued behind it.
    """
    try:
        from tesseract.config.loader import ConfigError, load_config

        return load_config()
    except (FileNotFoundError, OSError, ConfigError) as exc:
        log.warning("agents route: load_config failed (%s); returning unresolved model_role", exc)
        return None


def _resolve_model_ref(model_role: str, bundle: Any | None) -> str | None:
    """Return dotted ``<tier>.<provider>.<model>`` ref, or ``None`` on config error."""
    if bundle is None:
        return None
    try:
        from tesseract.config.loader import ConfigError

        if model_role in bundle.roles:
            return bundle.role(model_role).primary.ref
        return bundle.resolve(model_role).ref
    except (KeyError, ValueError, AttributeError, ConfigError):
        return None


def _serialize_agent(
    agent: AgentDefinition, *, status: str, bundle: Any | None = None
) -> dict[str, Any]:
    return {
        "name": agent.name,
        "description": agent.description,
        "version": agent.version,
        "model_role": agent.model_role,
        "resolved_ref": _resolve_model_ref(agent.model_role, bundle),
        "tools": list(agent.tools) if agent.tools is not None else None,
        "status": status,
        "max_tokens_override": agent.max_tokens_override,
        "disabled": agent.disabled,
        # Which half owns the card, and whether the operator's copy is
        # covering a shipped one. Read off the resolution rather than a
        # naming convention, so the surface cannot claim an ownership the
        # loader disagrees with.
        "origin": agent.origin,
        "shadows_system": agent.shadows_system,
    }


def _collect(names: list[str], *, status: str, include_pending: bool) -> dict[str, Any]:
    """Load and serialize a set of agents against ONE config bundle.

    Runs whole in a worker thread: every step is synchronous file IO and YAML
    parsing, which is exactly the work that must not sit on the event loop.
    """
    bundle = _load_bundle()
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for name in names:
        try:
            agent = load_agent(name, include_pending=include_pending)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        out.append(_serialize_agent(agent, status=status, bundle=bundle))
    return {"agents": out, "errors": errors}


async def list_agents_handler(request: web.Request) -> web.Response:
    """GET /api/agents — active agents (frontmatter summary)."""
    names = await asyncio.to_thread(list_agents)
    payload = await asyncio.to_thread(
        _collect, names, status="active", include_pending=False
    )
    return web.json_response(payload)


async def list_pending_handler(request: web.Request) -> web.Response:
    """GET /api/agents/pending — quarantined agents (frontmatter summary)."""
    names = await asyncio.to_thread(list_pending_agents)
    payload = await asyncio.to_thread(
        _collect, names, status="pending", include_pending=True
    )
    return web.json_response(payload)


async def get_agent_handler(request: web.Request) -> web.Response:
    """GET /api/agents/{name} — full frontmatter + sections."""
    name = request.match_info["name"]
    try:
        agent = load_agent(name, include_pending=True)
    except FileNotFoundError:
        return web.json_response({"error": f"agent {name!r} not found"}, status=404)
    status = "pending" if name in list_pending_agents() else "active"
    payload = _serialize_agent(agent, status=status)
    payload["sections"] = dict(agent.sections)
    return web.json_response(payload)


async def get_agent_source_handler(request: web.Request) -> web.Response:
    """GET /api/agents/{name}/source — raw .md file (frontmatter + body)."""
    name = request.match_info["name"]
    try:
        path = resolve_agent_path(name, include_pending=True)
    except FileNotFoundError:
        return web.json_response({"error": f"agent {name!r} not found"}, status=404)
    return web.json_response(
        {
            "name": name,
            "path": str(path),
            "source": path.read_text(encoding="utf-8"),
        }
    )


async def save_agent_source_handler(request: web.Request) -> web.Response:
    """POST /api/agents/{name}/source — body ``{source: str}``.

    Validates the new source (frontmatter, model_role, tools shape) before
    overwriting the .md atomically. Returns 400 with the validation error
    on failure; the on-disk file is left unchanged.
    """
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        return web.json_response({"error": "source must be a non-empty string"}, status=400)
    try:
        location = save_agent_source(name, source)
    except FileNotFoundError:
        return web.json_response({"error": f"agent {name!r} not found"}, status=404)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    payload: dict[str, Any] = {
        "name": name, "path": str(location.path), "saved": True,
        "origin": location.origin, "shadows_system": location.shadows_system,
    }
    if location.shadows_system and not location.extends:
        # Editing a shipped card's body forks it. Said here, at the moment it
        # happens, because from now on that agent stops receiving the
        # improvements an update brings — and a silent fork is how the
        # freeze this phase removed would grow back one card at a time.
        payload["notice"] = (
            f"{name} is a system agent. Your edit is saved as your own copy "
            "and no longer follows app updates. Delete it to go back to the "
            "shipped version."
        )
    return web.json_response(payload)


async def toggle_agent_disabled_handler(request: web.Request) -> web.Response:
    """POST /api/agents/{name}/toggle — body ``{disabled: bool}``."""
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("disabled"), bool):
        return web.json_response(
            {"error": "body must be {disabled: bool}"}, status=400
        )
    try:
        set_agent_disabled(name, bool(body["disabled"]))
    except FileNotFoundError:
        return web.json_response({"error": f"agent {name!r} not found"}, status=404)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    agent = load_agent(name, include_pending=True)
    status = "pending" if name in list_pending_agents() else "active"
    return web.json_response(_serialize_agent(agent, status=status))


__all__ = [
    "get_agent_handler",
    "get_agent_source_handler",
    "list_agents_handler",
    "list_pending_handler",
    "save_agent_source_handler",
    "toggle_agent_disabled_handler",
]
