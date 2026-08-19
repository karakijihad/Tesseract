"""Mirror Channels tab REST routes — MO-9-11 (Status + Logs) + MO-9-12 (Users + Conversations).

Endpoint surface:
    GET  /api/channels                                       — list channels + status snapshot
    GET  /api/channels/{name}/users                          — allowlist + pending + blocked
    GET  /api/channels/{name}/users/{user_id}/conversation   — JSONL tail for a chat
    POST /api/channels/{name}/restart                        — ASK-gated bridge bounce
    POST /api/channels/{name}/approve                        — ASK-gated; writes person record
    POST /api/channels/{name}/revoke                         — ASK-gated allowlist removal
    POST /api/channels/{name}/block                          — ASK-gated block (no-reply)
    POST /api/channels/telegram/status                       — ASK-gated online/offline override

The route layer talks to the :class:`ChannelAdapter` protocol (never to a
concrete bridge), so a future WhatsApp / Signal / Discord adapter ships
without route edits. Approve additionally writes a person record under
``memory-store/reference/people/<slug>.md`` so the operator has one row per
identity across channels (see ``_shared/channel-adapter-protocol.md``
§"Person-record cross-link"). The route is the canonical write site for that
record today — concrete adapters do not own it.

Mutating endpoints loop through the operator's chat-session ``ask_fn`` to
record an entry in ``approvals.jsonl`` under ``posture_source =
"channel_mutation"``. The Mirror UI button click is what the operator sees;
the ASK round-trip exists for the audit ledger and the symmetry with other
operator-attended mutations (e.g. ``agent_promote``).

For the Telegram offline override specifically, the bridge re-reads
``status.json`` on every inbound message tick — flipping the override does
NOT require a restart. ``state.py::save_status`` is the single writer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from typing import Any

from aiohttp import web
from pydantic import BaseModel

from tesseract.integrations import (
    ChannelAdapter,
    get_channel,
    list_channels,
)
from tesseract.integrations._person_record import upsert_person_record
from tesseract.kernel.tools.base import PermissionResult, ToolContext

log = logging.getLogger(__name__)


_VALID_TELEGRAM_OVERRIDES: frozenset[str] = frozenset({"online", "offline"})
_VALID_TIERS: frozenset[str] = frozenset({"operator", "friend"})
_CONVERSATION_LIMIT_CAP = 500


# ── ASK-gating shim ────────────────────────────────────────────────────────


class _ChannelOpTool:
    """Minimal Tool-shaped object so the chat session's ``ask_fn`` can fire
    a ``tool_ask`` envelope for a channel mutation that isn't a real Tool.

    ``ask_fn`` only touches ``tool.name`` (for the envelope payload) and
    ``validated.model_dump()`` (for the input summary). Synthesising those
    two surfaces lets the operator's existing Mirror approval modal handle
    channel restarts and offline-toggle flips without standing up a parallel
    approval lane.
    """

    default_posture = "ask"

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, *args: Any, **kwargs: Any) -> PermissionResult:
        del args, kwargs
        return PermissionResult.ASK


async def _require_channel_approval(
    app: web.Application,
    *,
    session_id: str,
    op_name: str,
    raw_input: dict[str, Any],
) -> tuple[bool, str, str | None]:
    """Drive the chat session's ASK round-trip for a channel mutation.

    Returns ``(approved, outcome, error_msg)``. ``error_msg`` is non-None
    only on setup failure (no session attached, no chat session, no ask_fn);
    callers translate that into a 503. ``outcome`` is whatever the asker
    claimed on the way out (``ToolContext.ask_outcome``) — empty when it
    claimed nothing — and exists so a refusal can be reported as the event
    it was rather than as a decision nobody made.
    """
    server_session = (app.get("server_sessions") or {}).get(session_id)
    if server_session is None:
        return False, "", f"operator session {session_id!r} not connected"
    chat_session = getattr(server_session, "chat_session", None)
    ask_fn = getattr(chat_session, "ask_fn", None) if chat_session is not None else None
    if ask_fn is None:
        return False, "", f"operator session {session_id!r} has no approval channel"

    call_id = uuid.uuid4().hex[:12]
    context = ToolContext(
        session_id=session_id,
        ask_fn=ask_fn,
        current_call_id=call_id,
        posture_source="channel_mutation",
    )

    class _Input(BaseModel):
        model_config = {"extra": "allow"}

    validated = _Input(**raw_input)
    tool = _ChannelOpTool(op_name)

    try:
        approved = bool(await ask_fn(tool, validated, context))
    except Exception:
        log.exception("channel approval: ask_fn raised for op=%s", op_name)
        # ask_fn implementations record their own row; this branch is a
        # transport-level failure (ws closed mid-prompt, etc.) — surface
        # to the caller as denied without a duplicate ledger row.
        return False, "error", "approval channel error"
    return approved, context.ask_outcome, None


#: Outcomes that mean the prompt ran out rather than that anyone answered it.
_UNANSWERED = frozenset({"timeout", "park_timeout"})


def _not_approved_response(action: str, outcome: str) -> web.Response:
    """Report a refused mutation without inventing a decision.

    The operator approves a channel user from the Channels view, but the
    prompt the round trip raises renders on the CHAT surface — so the
    common way this ends is nobody seeing it and the 30s window closing.
    Reported as "operator declined", that told the operator they had
    refused something they were never shown.
    """
    if outcome in _UNANSWERED:
        return web.json_response(
            {
                "status": "timeout",
                "output": (
                    f"no answer — the approval prompt for {action} expired "
                    "before it was answered. It appears on the chat surface; "
                    "keep that in view and try again."
                ),
            }
        )
    if not outcome:
        # An asker that made no claim (the channel gate nudges a workspace
        # inbox and returns False without deciding anything). Name both — and
        # take its own status rather than "denied", because the surface
        # prefixes a denial with the word and this sentence exists precisely
        # to say it does not know which of the two happened.
        return web.json_response(
            {
                "status": "unresolved",
                "output": (
                    f"{action} was not approved — the operator declined it, or "
                    "the approval prompt expired before it was answered."
                ),
            }
        )
    return web.json_response(
        {"status": "denied", "output": f"operator declined {action}"}
    )


# ── Serialization ──────────────────────────────────────────────────────────


def _snapshot_payload(adapter: ChannelAdapter) -> dict[str, Any]:
    snapshot = asdict(adapter.status_snapshot())
    extras: dict[str, Any] = {}
    if adapter.name == "telegram":
        # The Telegram offline override is not a generic ChannelStatus
        # field — surfacing it here lets the Status pane render the
        # Online/Offline toggle without a second round-trip. Reading
        # straight off the bridge's StateBundle keeps the route layer
        # ignorant of the on-disk JSON shape.
        try:
            state = getattr(adapter, "_state", None)
            status = getattr(state, "status", None) if state is not None else None
            extras["override"] = getattr(status, "override", None)
        except Exception:
            extras["override"] = None
    return {
        "name": adapter.name,
        "status_snapshot": snapshot,
        "extras": extras,
    }


# ── Handlers ───────────────────────────────────────────────────────────────


async def list_channels_handler(request: web.Request) -> web.Response:
    """``GET /api/channels`` — every registered adapter with its snapshot.

    Read-only and unauthenticated within the Mirror surface; the registry
    is process-local so no cross-tenant leakage exists.
    """
    del request
    payload = [_snapshot_payload(a) for a in list_channels()]
    return web.json_response({"channels": payload})


async def restart_channel_handler(request: web.Request) -> web.Response:
    """``POST /api/channels/{name}/restart`` — un-gated bridge bounce.

    2026-05-16: dropped the ASK gate. A bridge restart is a recovery
    action (idempotent ``stop()`` + ``start()`` with no config change),
    not a state mutation that needs operator approval. The pre-fix gate
    failed whenever the cockpit WS wasn't attached at the moment the
    operator clicked Restart — exactly when recovery was most needed.
    Allowlist / status / approve / revoke / block remain ASK-gated
    because those *do* alter operator-visible state.

    Body: ``{session_id?: str}`` (kept optional so existing UIs that
    send it don't break, but no longer validated). Calls
    ``adapter.stop()`` then ``adapter.start()``; surfaces the
    post-restart snapshot so the UI does not need a follow-up GET. A
    ``stop()`` failure short-circuits before ``start()`` to avoid
    double-spawning a poll task.
    """
    name = request.match_info["name"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)

    try:
        await adapter.stop()
    except Exception as exc:
        log.exception("channel restart: stop() failed for %s", name)
        return web.json_response(
            {"status": "denied", "output": f"adapter.stop() raised: {exc}"},
            status=500,
        )
    try:
        await adapter.start()
    except Exception as exc:
        log.exception("channel restart: start() failed for %s", name)
        return web.json_response(
            {"status": "denied", "output": f"adapter.start() raised: {exc}"},
            status=500,
        )
    return web.json_response(
        {
            "status": "approved",
            "output": f"{name} restarted",
            "channel": _snapshot_payload(adapter),
        }
    )


async def set_telegram_status_handler(request: web.Request) -> web.Response:
    """``POST /api/channels/telegram/status`` — ASK-gated offline toggle.

    Body: ``{session_id: str, override: "online" | "offline" | null}``.
    Writes ``telegram/status.json`` via ``state.py::save_status``. The
    Telegram bridge re-reads the file on every inbound message tick, so
    no bridge restart is required for the flip to take effect — an
    inbound message after a flip to ``offline`` auto-replies with
    "the assistant is offline right now. Your message was queued.".
    """
    adapter = get_channel("telegram")
    if adapter is None:
        return web.json_response(
            {"error": "telegram channel not registered"}, status=404
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"error": "session_id required (operator chat session)"}, status=400
        )

    raw_override = body.get("override", None)
    if raw_override is not None and (
        not isinstance(raw_override, str) or raw_override not in _VALID_TELEGRAM_OVERRIDES
    ):
        return web.json_response(
            {
                "error": (
                    "override must be 'online', 'offline', or null"
                )
            },
            status=400,
        )
    new_override = raw_override if isinstance(raw_override, str) else None

    approved, outcome, err = await _require_channel_approval(
        request.app,
        session_id=session_id,
        op_name="channel_status:telegram",
        raw_input={"channel": "telegram", "override": new_override},
    )
    if err is not None:
        return web.json_response({"error": err}, status=503)
    if not approved:
        return _not_approved_response("status change", outcome)

    # Late imports keep the route layer independent of the concrete bridge's
    # private state shape — Mirror restart on a fresh checkout starts before
    # the Telegram bridge stands up, and `state.py` is purely a file writer.
    from tesseract.integrations.telegram.state import (
        Status,
        load_status,
        save_status,
        telegram_state_dir,
    )

    status_path = telegram_state_dir() / "status.json"
    # Round-trip through load → mutate → save so a forward-compatible
    # extra field on disk survives the toggle (we only own `override`).
    status = load_status(status_path)
    status.override = new_override  # type: ignore[assignment]
    save_status(status_path, status)

    # Best-effort mirror of the new override into the live bridge so an
    # /status reply fires the right text the very next message tick. The
    # bridge would re-read on the next inbound anyway; this short-circuits
    # the window between save and first message.
    state = getattr(adapter, "_state", None)
    if state is not None:
        live_status = getattr(state, "status", None)
        if live_status is not None:
            try:
                live_status.override = new_override  # type: ignore[assignment]
            except Exception:
                pass

    # M1 — drain the offline inbox when flipping to online. Replay
    # happens in a background task so the route returns quickly; failure
    # to drain (network blip, decoder crash) logs but does not surface
    # to the operator as a route error.
    drained: int = 0
    if new_override != "offline":
        drainer = getattr(adapter, "drain_offline_inbox", None)
        if callable(drainer):
            try:
                drained = await drainer()
            except Exception:
                log.exception("channel status: offline drain raised for %s", adapter.name)

    return web.json_response(
        {
            "status": "approved",
            "output": f"telegram override set to {new_override!r}",
            "channel": _snapshot_payload(adapter),
            "drained": drained,
        }
    )


# ── MO-9-12: Users + Conversations panes ───────────────────────────────────


def _user_payload(adapter: ChannelAdapter) -> dict[str, Any]:
    return {
        "name": adapter.name,
        "users": [asdict(u) for u in adapter.list_users()],
    }


async def list_channel_users_handler(request: web.Request) -> web.Response:
    """``GET /api/channels/{name}/users`` — adapter ``list_users()`` projection."""
    name = request.match_info["name"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)
    return web.json_response(_user_payload(adapter))


async def get_channel_conversation_handler(request: web.Request) -> web.Response:
    """``GET /api/channels/{name}/users/{user_id}/conversation?limit=N&before_iso=...``.

    Reads from the per-channel conversation store via the adapter; no
    workspace-events round-trip (channels are transient brainstorm surfaces —
    see phase doc §1 architecture decision)."""
    name = request.match_info["name"]
    user_id = request.match_info["user_id"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)

    raw_limit = request.query.get("limit", "100")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return web.json_response({"error": "limit must be an integer"}, status=400)
    if limit < 0:
        return web.json_response({"error": "limit must be >= 0"}, status=400)
    if limit > _CONVERSATION_LIMIT_CAP:
        limit = _CONVERSATION_LIMIT_CAP

    before_iso = request.query.get("before_iso") or None

    try:
        rows = adapter.list_conversation(user_id, limit=limit, before_iso=before_iso)
    except ValueError as exc:
        # Concrete adapters raise ValueError on malformed user_id (e.g. the
        # Telegram bridge coerces to int). Surface as 400 so the Mirror UI
        # can show "Invalid user id" rather than a generic 500.
        return web.json_response({"error": str(exc)}, status=400)

    return web.json_response(
        {
            "name": name,
            "user_id": user_id,
            "rows": rows,
            "limit": limit,
            "before_iso": before_iso,
        }
    )


async def _parse_user_mutation_body(
    request: web.Request,
    *,
    require_tier: bool,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Shared payload parser for ``/approve`` ``/revoke`` ``/block``.

    Returns ``(body, None)`` on success or ``(None, response)`` to short-circuit
    the handler with a 400. ``require_tier=True`` is the approve-only path.
    """
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "body must be a JSON object"}, status=400
        )
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None, web.json_response(
            {"error": "session_id required (operator chat session)"}, status=400
        )
    user_id = body.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None, web.json_response({"error": "user_id required"}, status=400)
    if require_tier:
        tier = body.get("tier")
        if not isinstance(tier, str) or tier not in _VALID_TIERS:
            return None, web.json_response(
                {"error": "tier must be 'operator' or 'friend'"}, status=400
            )
        ttl_iso = body.get("ttl_iso", None)
        if ttl_iso is not None and not isinstance(ttl_iso, str):
            return None, web.json_response(
                {"error": "ttl_iso must be a string or null"}, status=400
            )
        display_name = body.get("display_name", None)
        if display_name is not None and not isinstance(display_name, str):
            return None, web.json_response(
                {"error": "display_name must be a string or null"}, status=400
            )
    return body, None


async def approve_channel_user_handler(request: web.Request) -> web.Response:
    """``POST /api/channels/{name}/approve`` — ASK-gated approve + person record.

    Body: ``{session_id, user_id, tier, ttl_iso?, display_name?}``. The route
    writes BOTH the adapter's own allowlist (``adapter.approve()``) AND a
    person record (``memory-store/reference/people/<slug>.md``). Person-record
    failure does not roll back the allowlist write — the channel is still
    usable; the record is a cross-channel identity link, not a gate. The
    failure surfaces in the response under ``person_record_error`` so the
    operator sees the partial state.
    """
    name = request.match_info["name"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)

    body, err_resp = await _parse_user_mutation_body(request, require_tier=True)
    if err_resp is not None:
        return err_resp
    assert body is not None

    session_id = body["session_id"]
    user_id = body["user_id"]
    tier = body["tier"]
    ttl_iso = body.get("ttl_iso") or None
    display_name = body.get("display_name") or None

    approved, outcome, err = await _require_channel_approval(
        request.app,
        session_id=session_id,
        op_name=f"channel_approve:{name}",
        raw_input={
            "channel": name,
            "user_id": user_id,
            "tier": tier,
            "ttl_iso": ttl_iso,
            "display_name": display_name,
        },
    )
    if err is not None:
        return web.json_response({"error": err}, status=503)
    if not approved:
        return _not_approved_response("approve", outcome)

    try:
        user = await adapter.approve(
            user_id,
            tier=tier,  # type: ignore[arg-type]
            ttl_iso=ttl_iso,
            display_name=display_name,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("channel approve: adapter.approve raised for %s", name)
        return web.json_response(
            {"status": "denied", "output": f"adapter.approve() raised: {exc}"},
            status=500,
        )

    person_record_error: str | None = None
    person_record_path: str | None = None
    try:
        path = upsert_person_record(
            channel=name,
            user_id=user_id,
            tier=tier,
            ttl_iso=ttl_iso,
            display_name=display_name,
        )
        person_record_path = str(path)
    except Exception as exc:
        log.exception("channel approve: person-record upsert failed for %s/%s", name, user_id)
        person_record_error = f"person record upsert failed: {exc}"

    return web.json_response(
        {
            "status": "approved",
            "output": f"{name}:{user_id} approved as {tier}",
            "user": asdict(user),
            "person_record_path": person_record_path,
            "person_record_error": person_record_error,
        }
    )


async def revoke_channel_user_handler(request: web.Request) -> web.Response:
    """``POST /api/channels/{name}/revoke`` — ASK-gated allowlist removal.

    Body: ``{session_id, user_id}``. Drops the user from the adapter's
    allowlist; the next message from that chat falls back to the pending /
    re-approval flow. The person record is NOT removed — revoking access is
    distinct from forgetting the identity.
    """
    name = request.match_info["name"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)
    body, err_resp = await _parse_user_mutation_body(request, require_tier=False)
    if err_resp is not None:
        return err_resp
    assert body is not None

    approved, outcome, err = await _require_channel_approval(
        request.app,
        session_id=body["session_id"],
        op_name=f"channel_revoke:{name}",
        raw_input={"channel": name, "user_id": body["user_id"]},
    )
    if err is not None:
        return web.json_response({"error": err}, status=503)
    if not approved:
        return _not_approved_response("revoke", outcome)

    try:
        user = await adapter.revoke(body["user_id"])
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("channel revoke: adapter.revoke raised for %s", name)
        return web.json_response(
            {"status": "denied", "output": f"adapter.revoke() raised: {exc}"},
            status=500,
        )
    return web.json_response(
        {
            "status": "approved",
            "output": f"{name}:{body['user_id']} revoked",
            "user": asdict(user),
        }
    )


async def block_channel_user_handler(request: web.Request) -> web.Response:
    """``POST /api/channels/{name}/block`` — ASK-gated block (silently dropped).

    Body: ``{session_id, user_id}``. After block, the bridge's ``_handle_message``
    short-circuits on every future inbound — no reply, no pending record. The
    /ignore semantics from IRC.
    """
    name = request.match_info["name"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)
    body, err_resp = await _parse_user_mutation_body(request, require_tier=False)
    if err_resp is not None:
        return err_resp
    assert body is not None

    approved, outcome, err = await _require_channel_approval(
        request.app,
        session_id=body["session_id"],
        op_name=f"channel_block:{name}",
        raw_input={"channel": name, "user_id": body["user_id"]},
    )
    if err is not None:
        return web.json_response({"error": err}, status=503)
    if not approved:
        return _not_approved_response("block", outcome)

    try:
        user = await adapter.block(body["user_id"])
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("channel block: adapter.block raised for %s", name)
        return web.json_response(
            {"status": "denied", "output": f"adapter.block() raised: {exc}"},
            status=500,
        )
    return web.json_response(
        {
            "status": "approved",
            "output": f"{name}:{body['user_id']} blocked",
            "user": asdict(user),
        }
    )


async def list_channel_missed_handler(request: web.Request) -> web.Response:
    """``GET /api/channels/{name}/users/{user_id}/missed`` — offline-inbox tail.

    Returns the still-queued offline messages for a chat so the Channels
    tab can surface them as a "missed while offline" pane. Read-only;
    drain happens via the explicit replay endpoint.
    """
    name = request.match_info["name"]
    user_id = request.match_info["user_id"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)
    lister = getattr(adapter, "list_missed", None)
    if not callable(lister):
        return web.json_response(
            {"name": name, "user_id": user_id, "rows": [], "supported": False}
        )
    try:
        rows = lister(int(user_id))
    except (TypeError, ValueError):
        return web.json_response({"error": "user_id must be an integer"}, status=400)
    return web.json_response(
        {"name": name, "user_id": user_id, "rows": rows, "supported": True}
    )


async def replay_channel_missed_handler(request: web.Request) -> web.Response:
    """``POST /api/channels/{name}/users/{user_id}/missed/replay`` — ASK-gated drain.

    Body: ``{session_id}``. Drains the chat's offline inbox by replaying
    each saved message through the normal turn loop. Returns the count
    replayed so the UI can show a "drained N message(s)" toast.
    """
    name = request.match_info["name"]
    user_id = request.match_info["user_id"]
    adapter = get_channel(name)
    if adapter is None:
        return web.json_response({"error": f"channel {name!r} not found"}, status=404)
    drainer = getattr(adapter, "drain_offline_inbox", None)
    if not callable(drainer):
        return web.json_response(
            {"status": "denied", "output": "channel has no offline-inbox surface"},
            status=400,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"error": "session_id required (operator chat session)"}, status=400
        )

    approved, outcome, err = await _require_channel_approval(
        request.app,
        session_id=session_id,
        op_name=f"channel_missed_replay:{name}",
        raw_input={"channel": name, "user_id": user_id},
    )
    if err is not None:
        return web.json_response({"error": err}, status=503)
    if not approved:
        return _not_approved_response("replay", outcome)

    try:
        cid = int(user_id)
    except (TypeError, ValueError):
        return web.json_response({"error": "user_id must be an integer"}, status=400)
    try:
        drained = await drainer(chat_id=cid)
    except Exception as exc:
        log.exception("channel missed replay: drain raised for %s/%s", name, user_id)
        return web.json_response(
            {"status": "denied", "output": f"drain raised: {exc}"}, status=500
        )
    return web.json_response(
        {
            "status": "approved",
            "output": f"replayed {drained} message(s)",
            "drained": drained,
        }
    )


__all__ = [
    "list_channels_handler",
    "restart_channel_handler",
    "set_telegram_status_handler",
    "list_channel_users_handler",
    "get_channel_conversation_handler",
    "approve_channel_user_handler",
    "revoke_channel_user_handler",
    "block_channel_user_handler",
    "list_channel_missed_handler",
    "replay_channel_missed_handler",
]
