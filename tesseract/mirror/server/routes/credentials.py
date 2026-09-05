"""The assistant's own accounts — the operator's half of the credential store.

Same three rules the keys route keeps, for the same reasons:

* **No value ever leaves.** ``GET`` reports metadata and whether a value is
  stored. There is no reveal, and no endpoint here returns one under any
  circumstance, because unlike an MCP bearer token there is nothing an
  operator has to paste a stored credential into.
* **Default-deny on shape.** Every field is validated by the store's own
  pydantic model before anything is written, so a malformed injection or a
  missing host allowlist is a 400 rather than an entry that fails at use.
* **Writes are localhost-only.** Mirror binds loopback and the bind is the
  gate for reads, but a route that writes secrets keeps the check next to the
  thing being protected (``_localhost.py``).

``dpapi_available`` is the honest half. The store encrypts with the Windows
user's own key, so on any other platform a save cannot happen at all — the
panel says so up front rather than letting the operator type a token into a
field that will refuse it.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.credentials import dpapi
from tesseract.credentials.models import Credential, CredentialDraft, Injection
from tesseract.credentials.store import (
    CredentialStore,
    CredentialStoreError,
    TooManyFields,
    UnknownCredentialError,
    UnknownCredentialField,
)
from tesseract.mirror.server.routes._localhost import is_localhost_request

log = logging.getLogger(__name__)


def _report() -> dict[str, Any]:
    return {
        "credentials": CredentialStore().list_public(),
        "dpapi_available": dpapi.is_available(),
    }


async def _json_body(request: web.Request) -> Any:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a bad body is a 400, never a 500
        return web.json_response({"error": "body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    return body


async def credentials_status(request: web.Request) -> web.Response:
    try:
        return web.json_response(_report())
    except CredentialStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)


async def credentials_save(request: web.Request) -> web.Response:
    """POST /api/credentials — add or edit one account.

    Body: ``{"id": "cred-x"|null, "service", "account", "purpose",
    "injection": {"kind", "name", "prefix", "field"}, "allowed_hosts": [...],
    "values": {"<field>": "..."}}``.

    ``values`` carries only the boxes the operator actually typed into. A
    field left blank is a field left alone, which is the same line the Keys
    panel had to be pulled back to: an operator correcting a purpose must not
    thereby destroy the credential they were describing. Clearing has its own
    endpoint.
    """
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    body = await _json_body(request)
    if isinstance(body, web.Response):
        return body

    values = body.get("values") or {}
    if not isinstance(values, dict):
        return web.json_response(
            {"error": "values must be an object of field name to value"},
            status=400,
        )
    for name, item in values.items():
        if not isinstance(item, str) or not item.strip():
            return web.json_response(
                {
                    "error": f"the value for {name!r} must be a non-empty "
                    f"string, or left out to keep the stored one"
                },
                status=400,
            )

    store = CredentialStore()
    try:
        existing = store.list_public()
    except CredentialStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    credential_id = body.get("id")
    if credential_id is not None and not isinstance(credential_id, str):
        return web.json_response({"error": "id must be a string"}, status=400)
    known = {entry["id"] for entry in existing}
    if credential_id and credential_id not in known:
        return web.json_response(
            {"error": f"no credential with id {credential_id!r}"}, status=404
        )

    injection = body.get("injection")
    if not isinstance(injection, dict):
        return web.json_response(
            {"error": "injection must be an object with kind, name and prefix"},
            status=400,
        )

    service = body.get("service")
    if not isinstance(service, str) or not service.strip():
        return web.json_response({"error": "service is required"}, status=400)

    fields = {
        "service": service.strip(),
        "account": str(body.get("account") or "").strip(),
        "purpose": str(body.get("purpose") or "").strip(),
        "allowed_hosts": list(body.get("allowed_hosts") or []),
    }
    try:
        injection_model = Injection(**injection)
        if credential_id:
            # No `fields` here, and that is not an omission. `put` unions what
            # the store already holds with what the caller names, so a panel
            # that knows the labels but never the blobs cannot delete a field
            # by not mentioning it.
            entry = Credential(
                id=credential_id,
                injection=injection_model,
                # An edit never re-opens a request and never closes one. `put`
                # clears the flag when the last box is filled; an operator
                # saving metadata on a row still waiting leaves it waiting.
                requested=bool(
                    next(
                        (e["requested"] for e in existing if e["id"] == credential_id),
                        False,
                    )
                ),
                **fields,
            )
        else:
            # No id, because the store mints one inside the lock it writes
            # under. There is nothing here for the route to invent.
            entry = CredentialDraft(injection=injection_model, **fields)
    except Exception as exc:  # noqa: BLE001 — validation reported at the operator
        return web.json_response({"error": str(exc)}, status=400)

    # No host list is a legitimate state on the way in, not a refusal. The
    # panel asks a person for what a person knows, and which hosts a service
    # authenticates against is the assistant's job to propose through
    # `credential_setup`, where the operator approves it in one answer. A row
    # with no hosts is refused everywhere until then, which is the safe
    # direction: `resolve` checks the list before anything is sent.

    try:
        saved = (
            store.put(entry, values=values)
            if credential_id
            else store.create(entry, values=values)
        )
    except (UnknownCredentialField, TooManyFields) as exc:
        # Both are shape errors the operator can act on, so both answer the
        # way every other shape error here does rather than as a 500.
        return web.json_response({"error": str(exc)}, status=400)
    except (CredentialStoreError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001 — DPAPI refusing is the operator's news
        return web.json_response({"error": str(exc)}, status=500)

    # Ids and services only. Which credential changed is worth an audit line;
    # a value would be the whole secret in a log file that ships with bug
    # reports.
    log.info("credentials: saved %s (%s)", saved["id"], saved["service"])
    _tell_every_panel()
    return web.json_response({"credential": saved, "report": _report()})


def _tell_every_panel() -> None:
    """Say the credential listing is out of date, to every open panel.

    The window that made the change gets fresh state in this response and
    needs nothing. Any OTHER open Mirror tab does not, and would keep showing
    what it fetched on mount. The two tools that write credentials already
    announce it; a route that writes the same file and stays silent is the
    same panel going stale by a different door.
    """
    from tesseract.orchestrator.panel_refresh import CREDENTIALS_KEY, publish_stale

    publish_stale(CREDENTIALS_KEY)


async def credentials_clear(request: web.Request) -> web.Response:
    """POST /api/credentials/clear — drop what is stored, keep the account.

    Every box is emptied. The boxes themselves stay: they are what the
    assistant asked for, and forgetting the question along with the answer
    would mean asking again just to get them back.
    """
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    body = await _json_body(request)
    if isinstance(body, web.Response):
        return body

    credential_id = body.get("id")
    if not isinstance(credential_id, str) or not credential_id:
        return web.json_response({"error": "id required"}, status=400)
    try:
        cleared = CredentialStore().clear_value(credential_id)
    except UnknownCredentialError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except CredentialStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    log.info("credentials: cleared every value on %s", credential_id)
    _tell_every_panel()
    return web.json_response({"credential": cleared, "report": _report()})


async def credentials_remove(request: web.Request) -> web.Response:
    """POST /api/credentials/remove — forget the account entirely."""
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    body = await _json_body(request)
    if isinstance(body, web.Response):
        return body

    credential_id = body.get("id")
    if not isinstance(credential_id, str) or not credential_id:
        return web.json_response({"error": "id required"}, status=400)

    try:
        CredentialStore().remove(credential_id)
    except UnknownCredentialError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    except CredentialStoreError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    log.info("credentials: removed %s", credential_id)
    _tell_every_panel()
    return web.json_response({"removed": credential_id, "report": _report()})


def register(app: web.Application) -> None:
    app.router.add_get("/api/credentials", credentials_status)
    app.router.add_post("/api/credentials", credentials_save)
    app.router.add_post("/api/credentials/clear", credentials_clear)
    app.router.add_post("/api/credentials/remove", credentials_remove)


__all__ = [
    "credentials_clear",
    "credentials_remove",
    "credentials_save",
    "credentials_status",
    "register",
]
