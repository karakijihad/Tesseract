"""Chain endpoints — the shared failover orders every role now follows.

A role names a chain; the chain is the only place a catalog ref lives. That
makes two edits possible where there used to be one, and they are kept apart
on purpose:

* **edit the chain** — moves every role following it, which is the whole
  reason the alias exists;
* **point the role at a different chain** — moves that role alone.

Which one an operator meant is not something to infer from a dropdown, so the
verbs are separate and `used_by` rides every response.

Lives beside `settings.py` rather than inside it: that module is already the
longest in the tree, and chains are their own noun.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from aiohttp import web

from tesseract.brain.boot import (
    adapter_class_for,
    adapter_unavailable_reason,
    rebuild_adapters,
)
from tesseract.lib.yaml_io import round_trip_yaml
from tesseract.mirror.server.routes.settings import (
    _allowed_kinds_for_target,
    _providers_yaml_path,
    _roles_yaml_path,
)

log = logging.getLogger(__name__)


def _read_roles(app: web.Application) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(_roles_yaml_path(app).read_text(encoding="utf-8")) or {}


def _chain_users(roles_block: Mapping[str, Any]) -> dict[str, list[str]]:
    """chain name -> the roles that follow it, in declaration order."""
    users: dict[str, list[str]] = {}
    for role, cfg in (roles_block or {}).items():
        if isinstance(cfg, Mapping) and isinstance(cfg.get("chain"), str):
            users.setdefault(cfg["chain"], []).append(role)
    return users


def _load(app: web.Application):
    from tesseract.config.loader import load_config

    return load_config(
        providers_path=_providers_yaml_path(app), roles_path=_roles_yaml_path(app)
    )


def _availability(resolved: Any) -> dict[str, Any]:
    """`{available, reason}` for an entry `build_adapter` serves, else `{}`.

    The gate is `adapter_class_for` — `build_adapter`'s own dispatch, kept in
    step with it by a test — and it is asked FIRST for a reason a live panel
    found: a voice lane (`local_whisper`, `kokoro`) is built by the voice
    engine and never reaches `build_adapter`, so asking why `build_adapter`
    would refuse it answers about the wrong builder and renders a working
    lane as broken. An entry nobody here can speak for reports nothing, and
    the column stays blank rather than carrying a true sentence about a
    function that entry never calls.
    """
    try:
        adapter_class_for(resolved)
    except Exception:
        return {}
    unavailable = adapter_unavailable_reason(resolved)
    return {"available": unavailable is None, "reason": unavailable}


def _rebuild(app: web.Application) -> str | None:
    """Propagate to live sessions. The yaml is already committed by the time
    this runs, so a failure is reported rather than raised — the disk is
    canonical and the next session reopen picks it up regardless."""
    try:
        rebuild_adapters(app)
    except Exception as exc:  # noqa: BLE001
        log.exception("chains: rebuild_adapters raised after YAML committed")
        return f"live rebuild failed: {exc}"
    return None


async def get_chains(request: web.Request) -> web.Response:
    """GET /api/settings/chains — every chain, what it holds, who follows it.

    An entry `build_adapter` serves also carries whether one could be BUILT
    from it (`available`) and, when it could not, the runtime's own sentence
    for why (`reason`) — see `_availability` for which entries those are.
    Asked of `boot.adapter_unavailable_reason`, the gate `build_adapter`
    raises from, so this reports rather than predicts: a disabled tier, a
    provider switched off, a missing key, a CLI binary absent from PATH and an
    uninstalled Ollama each name the flag or variable that is false. Two
    failures, kept apart on the row: `resolved: false` means fix the ref,
    `available: false` means fix a switch or a key.
    """
    from tesseract.config.loader import ConfigError

    try:
        bundle = _load(request.app)
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    roles_raw = _read_roles(request.app)
    users = _chain_users(roles_raw.get("roles") or {})

    chains: list[dict[str, Any]] = []
    for name, refs in (roles_raw.get("chains") or {}).items():
        entries: list[dict[str, Any]] = []
        kind: str | None = None
        for ref in refs or []:
            try:
                resolved = bundle.resolve(str(ref))
            except ConfigError:
                # A ref that no longer resolves must stay VISIBLE — it is the
                # one the operator has to fix, and dropping it from the list is
                # how a chain silently shortens.
                entries.append({"ref": str(ref), "resolved": False})
                continue
            kind = kind or resolved.model.kind
            entries.append({
                **_availability(resolved),
                "ref": str(ref),
                "resolved": True,
                "tier": resolved.connection.tier,
                "provider": resolved.connection.name,
                "model": resolved.model.model,
                "kind": resolved.model.kind,
                "context_window": int(resolved.model.fields.get("context_window") or 0),
                "good_for": [
                    str(tag) for tag in (resolved.model.fields.get("good_for") or ())
                ],
            })
        chains.append({
            "name": str(name),
            "kind": kind,
            "entries": entries,
            "used_by": users.get(str(name), []),
        })

    return web.json_response({"chains": chains})


def _validate_refs(bundle: Any, refs: list[str]) -> str | None:
    """`None` when `refs` form a usable chain, else why they do not."""
    from tesseract.config.loader import ConfigError

    if not refs:
        return "a chain needs at least one entry"
    seen: set[str] = set()
    kinds: list[str] = []
    for ref in refs:
        if ref in seen:
            return f"'{ref}' appears twice — a chain is an order, not a set"
        seen.add(ref)
        try:
            kinds.append(bundle.resolve(ref).model.kind)
        except ConfigError as exc:
            return str(exc)
    if len(set(kinds)) > 1:
        return (
            f"this chain mixes kinds ({', '.join(sorted(set(kinds)))}). A chain is "
            f"a failover order, so every entry must answer the same request."
        )
    return None


async def set_chain(request: web.Request) -> web.Response:
    """POST /api/settings/chain — write one chain's order, or delete it.

    Body: `{name, refs}` replaces the ordered list; `{name, delete: true}`
    removes it. A chain a role still follows cannot be deleted: the role would
    name something absent and the backend would refuse to boot, which is worse
    than refusing the delete.
    """
    from tesseract.config.loader import ConfigError

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return web.json_response({"error": "name must be a non-empty string"}, status=400)
    name = name.strip()

    roles_path = _roles_yaml_path(request.app)
    roles_raw = _read_roles(request.app)
    users = _chain_users(roles_raw.get("roles") or {}).get(name, [])

    if body.get("delete"):
        if name not in (roles_raw.get("chains") or {}):
            return web.json_response({"error": f"no chain named '{name}'"}, status=404)
        if users:
            return web.json_response(
                {"error": f"'{name}' is still followed by {', '.join(users)}"},
                status=409,
            )
        round_trip_yaml(roles_path, lambda d: d["chains"].pop(name, None))
        return web.json_response({"name": name, "deleted": True})

    refs = body.get("refs")
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        return web.json_response({"error": "refs must be a list of strings"}, status=400)

    try:
        bundle = _load(request.app)
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    problem = _validate_refs(bundle, refs)
    if problem:
        return web.json_response({"error": problem}, status=400)

    # A chain that roles already follow may not change what it SERVES — every
    # one of them was wired on the promise of a kind, and this is the edit that
    # could quietly break all of them at once.
    if users:
        for role in users:
            allowed = _allowed_kinds_for_target(bundle, role)
            new_kind = bundle.resolve(refs[0]).model.kind
            if allowed and new_kind not in allowed:
                return web.json_response(
                    {
                        "error": (
                            f"'{name}' is followed by {role}, which needs "
                            f"{sorted(allowed)} — this order serves '{new_kind}'"
                        )
                    },
                    status=400,
                )

    def _apply(doc: Any) -> None:
        doc.setdefault("chains", {})[name] = list(refs)

    round_trip_yaml(roles_path, _apply)
    error = _rebuild(request.app)

    return web.json_response({
        "name": name,
        "refs": refs,
        "used_by": users,
        "applied": True,
        "live_update_failed": error is not None,
        "live_update_error": error,
    })


async def set_role_chain(request: web.Request) -> web.Response:
    """POST /api/settings/role-chain — point one role at a different chain.

    The other half of the pair: this moves one role and nothing else, where
    `set_chain` moves everything following the chain it edits.
    """
    from tesseract.config.loader import ConfigError

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    role = body.get("role")
    chain = body.get("chain")
    if not isinstance(role, str) or not role:
        return web.json_response({"error": "role must be a non-empty string"}, status=400)
    if not isinstance(chain, str) or not chain:
        return web.json_response({"error": "chain must be a non-empty string"}, status=400)

    roles_path = _roles_yaml_path(request.app)
    roles_raw = _read_roles(request.app)
    if role not in (roles_raw.get("roles") or {}):
        return web.json_response({"error": f"no role named '{role}'"}, status=404)
    chains_raw = roles_raw.get("chains") or {}
    if chain not in chains_raw:
        return web.json_response({"error": f"no chain named '{chain}'"}, status=404)

    try:
        bundle = _load(request.app)
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    new_refs = [str(r) for r in (chains_raw.get(chain) or [])]
    problem = _validate_refs(bundle, new_refs)
    if problem:
        return web.json_response(
            {"error": f"chain '{chain}' is unusable: {problem}"}, status=400
        )

    # The role goes on serving what it served, so the kind it needs is the kind
    # it already has. Offering an image chain to chat_brain is the mistake this
    # catches, and catching it here means the backend never boots against it.
    allowed = _allowed_kinds_for_target(bundle, role)
    new_kind = bundle.resolve(new_refs[0]).model.kind
    if allowed and new_kind not in allowed:
        return web.json_response(
            {
                "error": (
                    f"role '{role}' needs a chain of kind {sorted(allowed)}, but "
                    f"'{chain}' serves '{new_kind}'"
                )
            },
            status=400,
        )

    def _apply(doc: Any) -> None:
        cfg = doc["roles"][role]
        cfg.pop("primary", None)
        cfg.pop("fallbacks", None)
        cfg["chain"] = chain

    round_trip_yaml(roles_path, _apply)
    error = _rebuild(request.app)

    return web.json_response({
        "role": role,
        "chain": chain,
        "refs": new_refs,
        "applied": True,
        "live_update_failed": error is not None,
        "live_update_error": error,
    })


def register(app: web.Application) -> None:
    app.router.add_get("/api/settings/chains", get_chains)
    app.router.add_post("/api/settings/chain", set_chain)
    app.router.add_post("/api/settings/role-chain", set_role_chain)


__all__ = ["register", "get_chains", "set_chain", "set_role_chain"]
