"""API keys — what the operator can set without opening a file manager.

Every key here comes from ``.env.example``, which is also what seeded the
operator's ``.env``, so the list, the prose beside each key and the signup
link are read from the file the operator is editing rather than from a copy
kept in the frontend.

Three rules this route does not bend:

* **No value ever leaves.** ``GET`` reports presence, never content. The one
  exception is the token this route itself generates, returned once in the
  ``POST`` that creates it, because a bearer token the operator cannot read
  cannot be pasted into the client it exists for.
* **Default-deny on names.** A write is accepted only for a key the template
  declares. Without that, this endpoint would be a way to set arbitrary
  environment variables — ``TESSERACT_HOME`` among them — for the next boot.
* **Writes are localhost-only.** Mirror binds loopback and the bind is the
  gate for reads, but a route that writes secrets keeps the check next to the
  thing being protected (``_localhost.py``).

``pending_restart`` is the honest half. ``.env`` is read once at boot while
the rest of ``config/`` hot-reloads, so a key edited here does nothing until
a restart. It is computed by comparing the file against this process's own
environment — the comparison happens server-side and only its boolean result
is sent, so a stale key is visible without either value being.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from aiohttp import web

from tesseract import env_file
from tesseract.mirror.server.routes._localhost import is_localhost_request

log = logging.getLogger(__name__)


def _mcp_block() -> dict[str, Any]:
    """The clients and the verb surface from ``mcp.yaml``.

    Beside the tokens on purpose: a bearer token is meaningless until you can
    see what it unlocks, and the answer is exactly this file's verb map. Best
    effort — an unloadable MCP config must not take the whole keys view with
    it, since the keys above it are the reason a fresh install opens this
    section at all.
    """
    from tesseract.paths import config_dir

    try:
        from tesseract.config.mcp import load_mcp_config

        config = load_mcp_config(config_dir() / "mcp.yaml")
    except Exception as exc:  # noqa: BLE001 — reported, never raised at the operator
        return {"clients": [], "verbs": [], "endpoint": None, "error": str(exc)}

    values = env_file.read_values()
    clients = [
        {
            "name": client.name,
            "token_env": client.token_env,
            "trust_tier": client.trust_tier,
            "in_file": bool(values.get(client.token_env, "").strip()),
            "active": bool((os.environ.get(client.token_env) or "").strip()),
        }
        for client in config.clients
    ]
    verbs = [
        {"verb": verb, "posture": posture} for verb, posture in sorted(config.verbs.items())
    ]
    return {
        "clients": clients,
        "verbs": verbs,
        "endpoint": f"http://{config.server.host}:{config.server.port}/mcp",
        "error": None,
    }


def _build_report() -> dict[str, Any]:
    specs = env_file.parse_example()
    values = env_file.read_values()

    sections: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    pending = False
    for spec in specs:
        section = index.get(spec.section)
        if section is None:
            section = {"title": spec.section, "advanced": spec.advanced, "keys": []}
            index[spec.section] = section
            sections.append(section)
        in_file_value = values.get(spec.name, "").strip()
        active_value = (os.environ.get(spec.name) or "").strip()
        key_pending = in_file_value != active_value
        pending = pending or key_pending
        section["keys"].append({
            "name": spec.name,
            "description": spec.description,
            "signup_url": spec.signup_url,
            "in_file": bool(in_file_value),
            "active": bool(active_value),
            "pending_restart": key_pending,
        })

    return {
        "env_path": str(env_file.env_path()),
        "sections": sections,
        "pending_restart": pending,
        "mcp": _mcp_block(),
    }


async def env_keys_status(request: web.Request) -> web.Response:
    return web.json_response(_build_report())


def _writable_names() -> set[str]:
    return {spec.name for spec in env_file.parse_example()}


async def _json_body(request: web.Request) -> dict | web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    return body


async def env_keys_write(request: web.Request) -> web.Response:
    """POST /api/env-keys — write one or more templated keys into ``.env``.

    Body: ``{"updates": {"OPENAI_API_KEY": "sk-..."}}``. An empty string
    clears a key. Returns the refreshed report, which already reflects the
    write and therefore already says a restart is pending.
    """
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    body = await _json_body(request)
    if isinstance(body, web.Response):
        return body

    updates = body.get("updates")
    if not isinstance(updates, dict) or not updates:
        return web.json_response(
            {"error": "updates must be a non-empty object of NAME -> value"}, status=400
        )

    allowed = _writable_names()
    cleaned: dict[str, str] = {}
    for name, value in updates.items():
        if name not in allowed:
            return web.json_response(
                {"error": f"{name!r} is not a key declared in .env.example"}, status=400
            )
        if not isinstance(value, str):
            return web.json_response({"error": f"{name}: value must be a string"}, status=400)
        cleaned[name] = value.strip()

    try:
        written = env_file.set_values(cleaned)
    except OSError as exc:
        return web.json_response({"error": f"could not write .env: {exc}"}, status=500)

    # Names only. The audit trail of "which key changed" is worth having and
    # costs nothing; the value would be the whole secret in a log file that
    # ships with bug reports.
    log.info("env-keys: wrote %s", ", ".join(written))
    return web.json_response({"written": written, "report": _build_report()})


async def env_keys_generate(request: web.Request) -> web.Response:
    """POST /api/env-keys/generate — mint one MCP bearer token.

    Body: ``{"name": "TESSERACT_MCP_SECRET"}``, restricted to the token envs
    ``mcp.yaml`` actually declares. The token is written to ``.env`` and
    returned once: this is the only response in this module that carries a
    secret, and it does so because the operator has to paste it into the
    client the token was minted for.
    """
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    body = await _json_body(request)
    if isinstance(body, web.Response):
        return body

    name = body.get("name")
    if not isinstance(name, str) or not name:
        return web.json_response({"error": "name required"}, status=400)

    mcp = _mcp_block()
    token_envs = {client["token_env"] for client in mcp["clients"]}
    if name not in token_envs:
        return web.json_response(
            {"error": f"{name!r} is not an MCP client token in mcp.yaml"}, status=400
        )

    token = env_file.generate_token()
    try:
        env_file.set_values({name: token})
    except OSError as exc:
        return web.json_response({"error": f"could not write .env: {exc}"}, status=500)

    log.info("env-keys: generated a new token for %s", name)
    return web.json_response({"name": name, "token": token, "report": _build_report()})


def register(app: web.Application) -> None:
    app.router.add_get("/api/env-keys", env_keys_status)
    app.router.add_post("/api/env-keys", env_keys_write)
    app.router.add_post("/api/env-keys/generate", env_keys_generate)


__all__ = ["register", "env_keys_status", "env_keys_write", "env_keys_generate"]
