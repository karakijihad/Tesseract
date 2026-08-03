"""IPC message Pydantic models — `_shared/ipc-contract.md`.

Length-prefixed framed IPC via :mod:`tesseract.kernel.sandbox._ipc_frames`
(``encode_frame`` / ``decode_frame``). Each Pydantic model carries a literal
``msg`` discriminator (client→controller) or ``event`` discriminator
(controller→client push). Parse helpers route by discriminator; unknown
messages surface as :class:`ErrorPush`.

Wire format note (2026-05-27): the controller daemon and every shipped client
(``ipc_client.ControllerClient``, the dispatcher, the mission worker) moved
from the newline-delimited JSON of the 2026-05-24 TC-4 prototype to the
length-prefixed framing primitive — see ``Docs/Doclog/2026-05-27.md §Controller
IPC migrated to length-prefixed framing``. Frames carry a 4-byte LE uint32
length prefix (``<I``) so messages exceeding the asyncio StreamReader
line-buffer limit (64 KiB) round-trip cleanly. The X-1 mission-worker
migration (``Docs/Plan/tars-cockpit/phase-X-1-stale-ipc-fix.md``) closed
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
    # WS-3: hard coder constraint for the spawned session. When set, the
    # spawned controller removes the opposing delegate_* tool from its
    # registry and appends a HARD-RULE directive. None → YAML default.
    preferred_coder: Literal["claude", "codex"] | None = None


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

    The TUI sends this on clean exit (default) so the next ``tars``
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


class LaneOpenMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_open"] = "lane_open"
    request_id: str
    kind: Literal["claude", "codex"]
    mode: Literal["headless"] = "headless"
    model: str
    working_dir: str
    env: dict[str, str] | None = None
    read_only: bool = False


class LaneSendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_send"] = "lane_send"
    request_id: str
    lane_id: str
    message: str


class LaneReadMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_read"] = "lane_read"
    request_id: str
    lane_id: str
    since_cursor: str | None = None


class LaneStatusMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_status"] = "lane_status"
    request_id: str
    lane_id: str


class LaneAttachMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_attach"] = "lane_attach"
    request_id: str
    lane_id: str


class LaneCloseMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_close"] = "lane_close"
    request_id: str
    lane_id: str
    reason: str = "operator_close"


class LaneInterruptMessage(BaseModel):
    """M2 — cancel a lane's in-flight turn without closing the lane (steer)."""
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_interrupt"] = "lane_interrupt"
    request_id: str
    lane_id: str


class LaneListMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_list"] = "lane_list"
    request_id: str


# ── Named lanes (CV-1) ──────────────────────────────────────────────────────
# The NamedLaneManager (name→lane_id binding layer over LaneManager) lives
# in-process in the daemon's ControllerRuntime. CV-1 exposes ensure/get/list
# over IPC so Mirror can resolve + spawn the trio's `coder/claude` +
# `auditor/codex` lanes without hosting a LaneManager itself.


class LaneNamedEnsureMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_named_ensure"] = "lane_named_ensure"
    request_id: str
    name: str
    kind: Literal["claude", "codex"]
    model: str
    working_dir: str
    mode: Literal["headless"] = "headless"


class LaneNamedGetMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    msg: Literal["lane_named_get"] = "lane_named_get"
    request_id: str
    name: str


class LaneNamedListMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    client receives one so picker UIs (Mirror, second ``tars`` window,
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
    "named_ensure", "named_get", "named_list",
]


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
