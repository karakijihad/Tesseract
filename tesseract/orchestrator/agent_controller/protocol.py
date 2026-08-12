"""IPC message Pydantic models — `_shared/ipc-contract.md`.

Length-prefixed framed IPC via :mod:`tesseract.kernel.sandbox._ipc_frames`
(``encode_frame`` / ``decode_frame``). Each Pydantic model carries a literal
``msg`` discriminator (client→controller) or ``event`` discriminator
(controller→client push). Parse helpers route by discriminator; unknown
messages surface as :class:`ErrorPush`.

Wire format note (2026-05-27): the controller daemon and every shipped client
(``ipc_client.ControllerClient``, the dispatcher, the mission worker) moved
from the newline-delimited JSON of the 2026-05-24 TC-4 prototype to the
length-prefixed framing primitive — see the ``Controller
IPC migrated to length-prefixed framing``. Frames carry a 4-byte LE uint32
length prefix (``<I``) so messages exceeding the asyncio StreamReader
line-buffer limit (64 KiB) round-trip cleanly. The X-1 mission-worker
migration closed
the last raw ``readline`` caller.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError


# ── Client → Controller ────────────────────────────────────────────────────


class AuthMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auth: str


class AttachMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["attach"] = "attach"
    session_id: str | None = None
    mode: Literal["interactive", "observer"] = "interactive"
    from_offset: int = 0


class UserInputMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["user_input"] = "user_input"
    session_id: str
    text: str


class ApprovalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["approval"] = "approval"
    session_id: str
    tool_use_id: str
    approved: bool
    operator_note: str | None = None


class CancelWorkerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["cancel_worker"] = "cancel_worker"
    session_id: str
    worker_id: str


class DetachMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["detach"] = "detach"
    session_id: str


class NewSessionMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["new_session"] = "new_session"
    title: str | None = None
    mode: Literal[
        "chat", "autonomy", "scheduler"
    ] = "chat"
    # Mirrors the registry's `SessionOrigin` set in `sessions.py`. Each
    # dispatcher names its own provenance.
    origin: Literal[
        "cli", "mirror", "autonomy", "scheduler", "telegram"
    ] = "cli"
    # Hard seat constraint for the spawned session. When set, the spawned
    # controller keeps only that seat's delegate tool in its registry and
    # appends a HARD-RULE directive. None → every seat stays available.
    preferred_seat: str | None = None
    # The MCP client identity that asked for this session (`agent.assign`).
    # Defaults to the operator because every other dispatcher — autonomy, the
    # scheduler, a workspace card, the TUI — IS the operator's own runtime;
    # only the hub has a principal other than that to name.
    #
    # Spelled literally rather than imported from `lanes.principals`: this
    # module is the wire schema and nothing else in it reaches into `lanes/`.
    # A test binds the two spellings so the duplication cannot drift silently.
    owner_principal: str = "operator"


class ListSessionsMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["list_sessions"] = "list_sessions"


class DeleteSessionMessage(BaseModel):
    """Operator-initiated session deletion.

    The daemon refuses if any client is currently attached to the
    session (writer present in ``_sessions_attached``). Operators
    detach first via ``/quit`` or ``client.detach()`` and then send
    delete — keeps the contract small: no force-flag, no observer
    eviction logic, no race with a live PTY runner.

    On success the daemon unlinks the record + transcript and
    broadcasts a :class:`SessionDeletedPush` to every authenticated
    client so other observers can update their session lists.
    """

    model_config = ConfigDict(extra="forbid")
    msg: Literal["delete_session"] = "delete_session"
    session_id: str


class RenameSessionMessage(BaseModel):
    """Operator-initiated session rename (``/title <text>`` in the TUI).

    The daemon updates the registry record and broadcasts a
    :class:`SessionRenamedPush` so other attached clients reflect the
    new title in their pickers.
    """

    model_config = ConfigDict(extra="forbid")
    msg: Literal["rename_session"] = "rename_session"
    session_id: str
    title: str


class ShutdownMessage(BaseModel):
    """Operator-initiated daemon teardown.

    The TUI sends this on clean exit (default) so the next ``agent``
    invocation spawns a fresh daemon picking up any code edits — matches
    the claude/codex CLI's "close terminal → process gone" UX. Headless
    callers (autonomy / scheduler) never send this; they rely
    on the supervisor's lifecycle.

    The daemon's handler: close every PTY runner + IPC connection, stop
    the asyncio server, exit the process. There is no "drain in-flight
    turns" budget here — the operator asked for it; honoring that is the
    point.
    """

    model_config = ConfigDict(extra="forbid")
    msg: Literal["shutdown"] = "shutdown"


# ── Lane control (X-4 Session C) ────────────────────────────────────────────
# Every `lane.*` request carries a `request_id`; the daemon emits a single
# `LaneResultPush` whose `request_id` matches so the client can resolve the
# awaiting future. The `result` payload is verb-specific (lane_id / events
# list / LaneStatus dict / LaneSnapshot dict / etc.); shapes are documented
# inline at each handler.
#
# Every lane message also carries `caller_principal`: the MCP client identity
# the gateway resolved the bearer token to. It stays OPTIONAL on the wire and
# is refused by the daemon when absent, rather than defaulting — an omitted
# caller and a caller who named the operator must not be the same message, or
# every unattested call quietly acquires cross-scope administration.


class _LaneMessage(BaseModel):
    """Shared caller context for the `lane.*` family."""

    model_config = ConfigDict(extra="forbid")
    caller_principal: str | None = None


class LaneOpenMessage(_LaneMessage):
    msg: Literal["lane_open"] = "lane_open"
    request_id: str
    # Mirrors `lanes.models.LaneKind`. Spelled literally for the same
    # reason `owner_principal` is: this module is the wire schema and
    # reaches into nothing else. A test binds the two spellings.
    kind: Literal["claude", "codex", "api"]
    mode: Literal["headless"] = "headless"
    model: str
    working_dir: str
    env: dict[str, str] | None = None
    read_only: bool = False
    # Principals the lane is opened to collaborate with, beyond its owner.
    # On the wire because ownership is enforced in the daemon: a work scope
    # the caller cannot express over IPC is one that only exists in tests.
    shared_with: list[str] = Field(default_factory=list)


class LaneSendMessage(_LaneMessage):
    msg: Literal["lane_send"] = "lane_send"
    request_id: str
    lane_id: str
    message: str


class LaneReadMessage(_LaneMessage):
    msg: Literal["lane_read"] = "lane_read"
    request_id: str
    lane_id: str
    since_cursor: str | None = None


class LaneTurnReadMessage(_LaneMessage):
    """One turn-scoped poll of a lane's event stream.

    `lane_read` returns everything the lane emitted; this returns only what
    the named turn emitted, and says whether the lane ever issued that turn.
    The correlation rule stays daemon-side (one implementation) and the
    proxy's wait loop only accumulates."""

    msg: Literal["lane_turn_read"] = "lane_turn_read"
    request_id: str
    lane_id: str
    turn_id: str
    since_cursor: str | None = None


class LaneStatusMessage(_LaneMessage):
    msg: Literal["lane_status"] = "lane_status"
    request_id: str
    lane_id: str


class LaneAttachMessage(_LaneMessage):
    msg: Literal["lane_attach"] = "lane_attach"
    request_id: str
    lane_id: str


class LaneCloseMessage(_LaneMessage):
    msg: Literal["lane_close"] = "lane_close"
    request_id: str
    lane_id: str
    reason: str = "operator_close"


class LaneInterruptMessage(_LaneMessage):
    """Cancel a turn without closing the lane (steer).

    ``turn_id`` scopes the cancel to one turn. Omitted, it keeps the
    operator-steer meaning: stop whatever this lane is doing now."""
    msg: Literal["lane_interrupt"] = "lane_interrupt"
    request_id: str
    lane_id: str
    turn_id: str | None = None


class LaneListMessage(_LaneMessage):
    msg: Literal["lane_list"] = "lane_list"
    request_id: str


# ── Named lanes (CV-1) ──────────────────────────────────────────────────────
# The NamedLaneManager (name→lane_id binding layer over LaneManager) lives
# in-process in the daemon's ControllerRuntime. CV-1 exposes ensure/get/list
# over IPC so Mirror can resolve + spawn named lanes (e.g. `coder/claude`,
# `auditor/codex`) without hosting a LaneManager itself.


class LaneNamedEnsureMessage(_LaneMessage):
    msg: Literal["lane_named_ensure"] = "lane_named_ensure"
    request_id: str
    name: str
    # Mirrors `lanes.models.LaneKind`. Spelled literally for the same
    # reason `owner_principal` is: this module is the wire schema and
    # reaches into nothing else. A test binds the two spellings.
    kind: Literal["claude", "codex", "api"]
    model: str
    # None = the daemon resolves the active project's root. The resolution is
    # the controller's so an out-of-process caller cannot land a lane somewhere
    # the in-process path would not have.
    working_dir: str | None = None
    mode: Literal["headless"] = "headless"


class LaneNamedGetMessage(_LaneMessage):
    msg: Literal["lane_named_get"] = "lane_named_get"
    request_id: str
    name: str


class LaneNamedListMessage(_LaneMessage):
    msg: Literal["lane_named_list"] = "lane_named_list"
    request_id: str


class ReloadMessage(BaseModel):
    """TC-5 — drain in-flight turns then reload runtime config.

    Targets:
    * ``config``  — re-read ``providers.yaml`` / ``roles.yaml`` only
    * ``roles``   — alias for ``config`` (Mirror watcher sends one of the
      two depending on which file fired); both fan out to the same
      ``rebuild_adapters`` path
    * ``tools``   — rebuild the tool registry (e.g. ``permissions.yaml``)
    * ``all``     — config + roles + tools in one drain
    """

    model_config = ConfigDict(extra="forbid")
    msg: Literal["reload"] = "reload"
    target: Literal["config", "roles", "tools", "all"] = "all"


class ActivitySnapshotMessage(BaseModel):
    """AS-1 gap-a — client requests a full Activity-registry snapshot.

    Sent by the Mirror's ``ActivitySubscriber`` immediately after a (re)connect
    so a lane/session that was mid-flight before the socket existed is
    reconciled at once, instead of showing stale disk-seeded state until its
    next transition. The controller replies with a single
    :class:`ActivitySnapshotPush` to the requesting client only (not broadcast).
    """

    model_config = ConfigDict(extra="forbid")
    msg: Literal["activity_snapshot"] = "activity_snapshot"
    # The MCP-side `activity.list` is caller-scoped; this is its raw-IPC
    # counterpart and has to be too, or the snapshot is a way around the
    # filter. Refused when absent, exactly as the `lane.*` family is: an
    # omitted caller and a caller who named the operator must not be the same
    # message on one surface and different on its neighbour.
    caller_principal: str | None = None


# ── Controller-side ASK parking (Option B, 2026-07-13) ──────────────────────
# `request_permission` (daemon.py) parks an ASK — either immediately (no
# interactive client attached) or after its initial attended-wait expires
# (attached-but-silent) — instead of denying outright. The future stays
# daemon-owned throughout; Mirror only ever holds a VIEW of the parked set
# (populated via `ControllerAskParkedPush` / `ParkedAsksSnapshotPush`) and a
# verb to decide it (`DecideParkedAskMessage`), mirroring the `activity.*`
# snapshot/push pair.


class ParkedAsksSnapshotMessage(BaseModel):
    """Mirror's controller-parked-asks view requests a full snapshot of
    `_parked_asks` on (re)connect, mirroring :class:`ActivitySnapshotMessage`.
    The controller replies with a single :class:`ParkedAsksSnapshotPush` to
    the requesting client only (not broadcast)."""

    model_config = ConfigDict(extra="forbid")
    msg: Literal["parked_asks_snapshot"] = "parked_asks_snapshot"


class DecideParkedAskMessage(BaseModel):
    """Operator decision on a controller-side parked ask — sent by Mirror's
    ``POST /api/asks/{approval_id}/decision`` route when the target entry's
    origin is ``"controller"``. Unlike :class:`ApprovalMessage` this verb
    carries NO attach requirement; see ``_on_decide_parked_ask`` for the
    trust rationale (a parked ask by definition has no attached watcher)."""

    model_config = ConfigDict(extra="forbid")
    msg: Literal["decide_parked_ask"] = "decide_parked_ask"
    approval_id: str
    approved: bool
    operator_note: str | None = None


ClientMessage = Union[
    AttachMessage,
    UserInputMessage,
    ApprovalMessage,
    CancelWorkerMessage,
    DetachMessage,
    DeleteSessionMessage,
    NewSessionMessage,
    ListSessionsMessage,
    ReloadMessage,
    RenameSessionMessage,
    ShutdownMessage,
    LaneOpenMessage,
    LaneSendMessage,
    LaneReadMessage,
    LaneTurnReadMessage,
    LaneStatusMessage,
    LaneAttachMessage,
    LaneCloseMessage,
    LaneInterruptMessage,
    LaneListMessage,
    LaneNamedEnsureMessage,
    LaneNamedGetMessage,
    LaneNamedListMessage,
    ActivitySnapshotMessage,
    ParkedAsksSnapshotMessage,
    DecideParkedAskMessage,
]

_CLIENT_KINDS: dict[str, type[BaseModel]] = {
    "attach": AttachMessage,
    "user_input": UserInputMessage,
    "approval": ApprovalMessage,
    "cancel_worker": CancelWorkerMessage,
    "detach": DetachMessage,
    "delete_session": DeleteSessionMessage,
    "new_session": NewSessionMessage,
    "list_sessions": ListSessionsMessage,
    "reload": ReloadMessage,
    "rename_session": RenameSessionMessage,
    "shutdown": ShutdownMessage,
    "lane_open": LaneOpenMessage,
    "lane_send": LaneSendMessage,
    "lane_read": LaneReadMessage,
    "lane_turn_read": LaneTurnReadMessage,
    "lane_status": LaneStatusMessage,
    "lane_attach": LaneAttachMessage,
    "lane_close": LaneCloseMessage,
    "lane_interrupt": LaneInterruptMessage,
    "lane_list": LaneListMessage,
    "lane_named_ensure": LaneNamedEnsureMessage,
    "lane_named_get": LaneNamedGetMessage,
    "lane_named_list": LaneNamedListMessage,
    "activity_snapshot": ActivitySnapshotMessage,
    "parked_asks_snapshot": ParkedAsksSnapshotMessage,
    "decide_parked_ask": DecideParkedAskMessage,
}


class UnknownClientMessage(ValueError):
    """Raised when an authenticated client sends a `msg` discriminator
    that does not match any model in :data:`_CLIENT_KINDS`. The caller
    treats this the same as a Pydantic ``ValidationError`` — both
    surface as an `invalid_message` error push."""


def parse_client_message(payload: dict[str, Any]) -> ClientMessage:
    """Dispatch a raw JSON dict to its specific client message model.

    The `auth` handshake is parsed separately via :class:`AuthMessage` —
    callers must check for the `auth` key BEFORE invoking this function.
    Raises :class:`UnknownClientMessage` on unrecognised discriminator
    or :class:`pydantic.ValidationError` on schema mismatch."""
    msg = payload.get("msg")
    model = _CLIENT_KINDS.get(str(msg)) if isinstance(msg, str) else None
    if model is None:
        raise UnknownClientMessage(
            f"unknown message: {msg!r} (expected one of {sorted(_CLIENT_KINDS)})"
        )
    return model.model_validate(payload)


# ── Controller → Client (push) ─────────────────────────────────────────────


class AttachedPush(BaseModel):
    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["attached"] = "attached"
    session: dict[str, Any]
    replay_events: list[dict[str, Any]] = Field(default_factory=list)
    end_offset: int = 0


class TranscriptEventPush(BaseModel):
    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["transcript_event"] = "transcript_event"
    session_id: str
    transcript_event: dict[str, Any]
    end_offset: int = 0


class SessionListPush(BaseModel):
    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["session_list"] = "session_list"
    sessions: list[dict[str, Any]] = Field(default_factory=list)


class ErrorPush(BaseModel):
    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["error"] = "error"
    code: str
    detail: str = ""


class AckPush(BaseModel):
    """Generic ack for fire-and-forget client messages (user_input, approval,
    detach). The transcript_event push is the real source-of-truth — `ack`
    just tells the client the message was accepted by the dispatcher."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["ack"] = "ack"
    msg: str
    session_id: str | None = None


class SessionStatusPush(BaseModel):
    """TC-5 — controller broadcasts session lifecycle changes (most often
    ``idle`` during a reload drain, then ``active`` again once the reload
    completes). The shape is intentionally narrow: clients use it to
    surface a status badge, not to drive routing decisions."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["session_status"] = "session_status"
    session_id: str
    status: Literal["active", "idle", "detached", "closed"]
    reason: str | None = None


class SessionDeletedPush(BaseModel):
    """Broadcast on successful session deletion. Every authenticated
    client receives one so picker UIs (Mirror, second ``agent`` window,
    observer attach) can drop the row."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["session_deleted"] = "session_deleted"
    session_id: str


class SessionRenamedPush(BaseModel):
    """Broadcast on successful session rename so picker UIs refresh
    their cached title without round-tripping ``list_sessions``."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["session_renamed"] = "session_renamed"
    session_id: str
    title: str


class ActivityEventPush(BaseModel):
    """AS-1 — relays one Unified Activity Registry event from the controller
    daemon to every connected client. ``envelope`` is the verbatim activity
    envelope (``{kind, channel:"activity", session_id:<activity_id>, ts,
    data}``) the controller's bus produced; the Mirror's activity subscriber
    re-applies it to the Mirror-side registry. Broadcast to all (not
    session-scoped) — activity is operator-global, like surfaces."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["activity_event"] = "activity_event"
    envelope: dict[str, Any]


class ActivitySnapshotPush(BaseModel):
    """AS-1 gap-a — full Activity-registry snapshot, sent to the requesting
    client in reply to :class:`ActivitySnapshotMessage`. ``records`` is the list
    of ``ActivityRecordOut`` dicts from ``ActivityRegistry.snapshot()``; the
    subscriber upserts each into the Mirror-side registry. Unlike
    :class:`ActivityEventPush` this is point-to-point (the requesting client),
    not a broadcast."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["activity_snapshot"] = "activity_snapshot"
    records: list[dict[str, Any]] = Field(default_factory=list)


class ControllerAskParkedPush(BaseModel):
    """Broadcast when ``request_permission`` transitions an ASK into the
    parked state — either immediately (no interactive client attached) or
    after the initial attended-wait expires (attached-but-silent). Mirror's
    view store (``app["controller_parked_asks"]``) upserts on this push; the
    daemon stays authoritative throughout — deciding routes back through
    ``decide_parked_ask``, never mutates this push's data directly."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["controller_ask_parked"] = "controller_ask_parked"
    approval_id: str
    session_id: str
    tool: str
    summary: str
    tool_use_id: str
    parked_at: str


class ControllerAskSettledPush(BaseModel):
    """Broadcast once a parked ask resolves (approved/denied/park_timeout/
    cancelled) so every connected client — including Mirror's view store —
    drops the row. The daemon has already recorded the terminal transcript
    event by the time this fires; ``resolution`` is informational for the
    view, not load-bearing for correctness (removal keys off ``approval_id``
    alone)."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["controller_ask_settled"] = "controller_ask_settled"
    approval_id: str
    session_id: str
    tool_use_id: str
    resolution: str


class ParkedAsksSnapshotPush(BaseModel):
    """Reply to :class:`ParkedAsksSnapshotMessage` — the full current
    ``_parked_asks`` set, point-to-point (not broadcast), mirroring
    :class:`ActivitySnapshotPush`. ``items`` are ``_ParkedControllerAsk.
    to_wire()`` dicts."""

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["parked_asks_snapshot"] = "parked_asks_snapshot"
    items: list[dict[str, Any]] = Field(default_factory=list)


LaneVerb = Literal[
    "open", "send", "read", "status", "attach", "close", "interrupt", "list",
    "turn_read", "named_ensure", "named_get", "named_list",
]
"""Every verb a ``lane_result`` may name. A handler pushes its own verb into
this union, so a verb missing here does not degrade — the push fails Pydantic
validation inside the handler and NO result is sent, leaving the caller's
future to time out with nothing said. Add the verb here in the same change
that adds the handler."""


class LaneResultPush(BaseModel):
    """X-4 Session C — response to a `lane.*` request.

    The single polymorphic push avoids seven near-identical event types.
    `request_id` matches the originating message so the client can route
    to the right awaiting future. `verb` names which `lane.*` method
    produced this; `result` is the verb-specific payload (lane_id, events
    list, LaneStatus dict, LaneSnapshot dict, close-result dict, or the
    list of ids for `lane_list`). On failure, `ok=False`, `result` is
    empty, and `error` carries the message.
    """

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["lane_result"] = "lane_result"
    request_id: str
    verb: LaneVerb
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ReloadCompletePush(BaseModel):
    """TC-5 — controller emits once :class:`ReloadMessage` finishes.

    ``reloaded`` lists the targets that succeeded; ``failed`` lists those
    whose reload callback raised (one line each, ``"<target>: <error>"``).
    ``pending_turns`` is the count of dispatch turns that were still
    running when ``drain_timeout_seconds`` expired — the operator can
    decide whether to retry or accept the partial reload.
    """

    model_config = ConfigDict(extra="allow")
    push: Literal[True] = True
    event: Literal["reload_complete"] = "reload_complete"
    target: Literal["config", "roles", "tools", "all"]
    reloaded: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    session_count: int = 0
    pending_turns: int = 0
    drain_timeout_seconds: float = 0.0


__all__ = [
    "AckPush",
    "ActivitySnapshotMessage",
    "ActivitySnapshotPush",
    "ApprovalMessage",
    "AttachMessage",
    "AttachedPush",
    "AuthMessage",
    "CancelWorkerMessage",
    "ClientMessage",
    "ControllerAskParkedPush",
    "ControllerAskSettledPush",
    "DecideParkedAskMessage",
    "DeleteSessionMessage",
    "DetachMessage",
    "ErrorPush",
    "LaneAttachMessage",
    "LaneCloseMessage",
    "LaneInterruptMessage",
    "LaneListMessage",
    "LaneNamedEnsureMessage",
    "LaneNamedGetMessage",
    "LaneNamedListMessage",
    "LaneOpenMessage",
    "LaneReadMessage",
    "LaneTurnReadMessage",
    "LaneResultPush",
    "LaneSendMessage",
    "LaneVerb",
    "LaneStatusMessage",
    "ListSessionsMessage",
    "NewSessionMessage",
    "ParkedAsksSnapshotMessage",
    "ParkedAsksSnapshotPush",
    "ReloadCompletePush",
    "ReloadMessage",
    "RenameSessionMessage",
    "SessionDeletedPush",
    "SessionListPush",
    "SessionRenamedPush",
    "SessionStatusPush",
    "ShutdownMessage",
    "TranscriptEventPush",
    "UnknownClientMessage",
    "UserInputMessage",
    "parse_client_message",
]
