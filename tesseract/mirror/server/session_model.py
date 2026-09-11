"""Session data model — ServerSession, chat metadata, and envelope delivery."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from aiohttp import web

from tesseract.brain.chat import ChatSession
from tesseract.mirror.server.chat_record import default_chat_title
from tesseract.mirror.server.event_log import EventLog
from tesseract.mirror.server.voice_loop import VoiceLoop

log = logging.getLogger(__name__)


#: Who is on the other end of a conversation, which is not the same question as
#: which door a turn came through (`ChatSession.turn_entry`). `cockpit` and
#: `channel` both mean a person is waiting; `autonomy` means nobody is, and it
#: is the only one where an unanswered approval is a refusal on a timer rather
#: than someone taking their time.
#:
#: Every consumer branches on `== "channel"` and lets everything else fall to
#: the cockpit's behaviour, so a third kind inherits the attended path by
#: default. That is right for the tag parser and the retry rule and wrong for
#: exactly one thing, the memory leaf a turn leaves behind, which would
#: otherwise file unattended work as something said in the cockpit
#: (`chat.py::_emit_turn_leaf`).
SessionKind = Literal["cockpit", "channel", "autonomy"]

# mirror-multi-chat D5 — soft cap on simultaneously-open chats per session.
# Creating past the cap auto-archives the oldest open chat. UI warns near it.
MAX_OPEN_CHATS = 10


@dataclass
class ChatMeta:
    """Per-chat metadata held alongside the brain-layer ``ChatSession``.

    The ``ChatSession`` (history, observer, tools) lives in
    ``ServerSession.chats``; this carries the sidebar/persistence facts —
    operator-edited title, timestamps, archive state, turn tally. Mirrors
    the per-chat JSON schema (``tesseract/sessions/chats/<chat_id>.json``).
    """

    chat_id: str
    title: str
    created_at: str
    started_at: str
    archived: bool = False
    turn_count: int = 0
    # The adapter model this chat was last spoken to with. It belongs to the
    # chat rather than to a write: a writer that does not happen to know it —
    # rename, archive, restore — must not blank the record by saying nothing.
    model: str = ""


def _new_chat_meta(
    chat_id: str, now: datetime, *, title: str | None = None
) -> ChatMeta:
    # D2 default title is a local wall-clock date-stamp (callers pass a local
    # zone-aware `now`); the operator renames if they care.
    stamp = now.isoformat()
    return ChatMeta(
        chat_id=chat_id,
        title=title or default_chat_title(now),
        created_at=stamp,
        started_at=stamp,
    )


def stamp_chat_id(chat_session: Any, chat_id: str) -> None:
    """Tell a chat session which chat it is, at the one moment that is known.

    Every registration point calls it, so a chat rebuilt from disk on restore
    knows its id as surely as one created fresh. Anything the assistant leaves
    behind records this, which is how a press on a card drawn an hour ago
    reaches the conversation that drew it rather than whichever is in focus.

    Defensive on the attribute: several doubles stand in for a ChatSession, and
    a chat that cannot be stamped should still register.
    """
    context = getattr(chat_session, "tool_context", None)
    if context is not None:
        context.chat_id = chat_id


@dataclass
class ParkedAsk:
    """trio W4 — one background-spawn ASK that outlived its live window and
    now waits for the operator in the approvals pane (ask-instead-of-die).
    The future is the SAME one the chat card resolves — either surface
    settles it; the other sees a no-op."""

    call_id: str
    session_id: str
    tool_name: str
    input_summary: str
    spawn_handle_id: str | None
    parked_at: str
    future: "asyncio.Future[bool]" = field(repr=False)
    # M13 — a server-minted id that keys the app-level dict and is returned to
    # the UI. A provider `call_id` is only unique within its session, so two
    # sessions producing the same opaque call_id could otherwise overwrite or
    # settle each other's parked ask. The decision route resolves by this id.
    approval_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Controller-daemon ASK parking (Option B, 2026-07-13) — distinguishes a
    # chat-session (Mirror-process) parked ask from one relayed from the
    # controller daemon's own `_parked_asks` (routes/asks_parked.py merges
    # both stores). Every existing call-site constructs a chat-origin entry,
    # so the default keeps them unchanged.
    origin: str = "chat"

    def to_wire(self) -> dict[str, str | None]:
        return {
            "approval_id": self.approval_id,
            "call_id": self.call_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "input_summary": self.input_summary,
            "spawn_handle_id": self.spawn_handle_id,
            "parked_at": self.parked_at,
            "origin": self.origin,
        }


@dataclass
class ServerSession:
    session_id: str
    ws: web.WebSocketResponse
    chat_session: ChatSession
    event_log: EventLog
    pending_asks: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    # trio W4 — parked background-spawn asks (call_id → ParkedAsk), fed by
    # `_make_ask_fn`'s park branch, listed/settled via routes/asks_parked.py.
    # NOTE: in production this is the SHARED app-level dict
    # (`app["parked_asks"]`) so parked entries survive WS disconnect /
    # session cleanup — never a per-session lifetime.
    parked_asks: dict[str, "ParkedAsk"] = field(default_factory=dict)
    # Discriminator between the operator-facing cockpit session
    # (Mirror WebSocket; ``app["server_sessions"]``) and a headless
    # channel session (Telegram/WhatsApp/etc.; bridge-owned). The prompt
    # overlay and the ASK-gate behaviour both key off this.
    kind: SessionKind = "cockpit"
    turn_count: int = 0
    # The session is over: the socket went away and `cleanup_session` has
    # dropped it from every registry the app can reach it through.
    #
    # Read by `_run_turn`'s end-of-turn tail, which drains the operator's
    # queued follow-ups and starts the next turn. That drain runs on every way
    # a turn can end, including a cancel, because a stop must not throw away
    # what the operator typed. A teardown cancels turns too, and the queue it
    # leaves behind belongs to a session that no longer exists: draining it
    # spawns a real turn, at real cost, streaming to a closed socket, which no
    # later stop can reach because the session is not in `server_sessions` any
    # more. Set BEFORE the cancels, so the tail cannot run before the flag.
    torn_down: bool = False
    # Turn tasks keyed by chat_id (rather than a single
    # `current_turn_task`). The active chat's task is exposed via the
    # `current_turn_task` property+setter below, so every legacy reader/writer
    # (busy checks, channel-bridge driver, cancel, cleanup) keeps working
    # unchanged; conductor/background turns address other chats via the dict.
    current_turn_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    # A turn that has been ADMITTED but has not started yet. A channel turn
    # waits for the chat lock and then does awaited setup (the placeholder
    # send, link extraction) before `channel_turn` assigns
    # `current_turn_task`, and a stop arriving anywhere in that stretch used
    # to find nothing to cancel, answer "Nothing was running", and then watch
    # the turn start anyway. The admitting handler registers its own task here
    # for the whole of it, from BEFORE the lock: a handler still queued on the
    # lock is a turn the operator can see coming.
    #
    # A set per chat, not one task. Several handlers can be waiting at once,
    # and with a single slot the newest would overwrite the one that is
    # actually running, so a stop would cancel the turn that had not started
    # and miss the turn that had.
    #
    # Read by `stop.py`; deliberately NOT read by the busy checks, because
    # `_start_channel_turn` would find the caller's own task and wait for
    # itself.
    pending_turn_tasks: dict[str, set["asyncio.Task[None]"]] = field(
        default_factory=dict,
    )
    # Serializes the streaming body of ACTIVE-chat turns only (see
    # `_run_chat_turn`). TTS/stream-parser state is per-turn
    # now, so the lock's remaining job is audio ordering — a second active-chat
    # send waits for the first so the operator never hears two replies overlap.
    # Background chats stream lock-free; synthetic workspace turns do NOT take
    # this lock (they suppress text output).
    turn_stream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    started_at: str = ""
    # Tool name keyed by call_id, populated on TOOL_CALL_END, popped on TOOL_RESULT.
    # Lets `_handle_chunk` know which tool a result belongs to without scanning history
    # — needed so `set_mood` results can immediately trigger an entity_signals emission.
    tool_names_by_call: dict[str, str] = field(default_factory=dict)
    # Per-turn tool-call counter for the deep_focus auto-state. Reset at
    # turn start; bumped on every TOOL_CALL_END. When it crosses
    # `_DEEP_FOCUS_TOOL_THRESHOLD` we flip the orb to `deep_focus` once.
    turn_tool_count: int = 0
    deep_focus_latched: bool = False
    # Call-ids of in-flight `memory_save` tool calls whose input had
    # `importance >= 8`. Captured on TOOL_CALL_END, drained on TOOL_RESULT
    # to flip the orb to `happy` on a successful high-importance save.
    pending_happy_saves: set[str] = field(default_factory=set)
    # Background entity_signals pump task. Cancelled in websocket_handler.finally
    # before autosave so pump shutdown can't be skipped by an autosave exception.
    entity_signals_task: asyncio.Task[None] | None = None
    # Surface Protocol event forwarder pump (channel "surface").
    # Cancelled symmetrically in websocket_handler.finally.
    surface_events_task: asyncio.Task[None] | None = None
    # Unified Activity event forwarder pump (channel "activity").
    # Cancelled symmetrically in websocket_handler.finally.
    activity_events_task: asyncio.Task[None] | None = None
    # Panel cache-staleness forwarder pump (channel "panel").
    # Cancelled symmetrically in websocket_handler.finally.
    panel_events_task: asyncio.Task[None] | None = None
    # Autosave timer (`session_autosave`). Cancelled symmetrically in
    # websocket_handler.finally, before the teardown save.
    autosave_task: asyncio.Task[None] | None = None
    # Tallies for the `[session_end]` daily writer section (S1). Incremented
    # inline by `_maybe_auto_compact` / `cmd_reflect`; runtime-only.
    compact_count: int = 0
    memory_saves: int = 0
    # Wall-clock of the most recent `_run_turn` start. Seeded at session-open
    # so a fresh, untouched session isn't instantly considered idle.
    last_turn_at: datetime | None = None
    # Per-session voice PCM accumulator. Lazily allocated on the
    # first BINARY frame; `voice_commit` drains it through `STTEngine`,
    # `voice_cancel` clears it. Capped at `VOICE_PCM_BUFFER_CAP_BYTES` (5 min
    # at 16 kHz/16-bit mono = 9_600_000 bytes). Excess frames trim the head.
    voice_pcm_buffer: bytearray | None = None
    # Bumped by every operator cancel. `voice_commit` reads it on the way in
    # and again after transcribing, and drops the utterance if it moved.
    #
    # Clearing the buffer is not enough on its own: by the time a cancel
    # arrives the commit handler usually holds its own reference to the audio
    # and is inside STT, so the buffer it clears is one nothing is reading.
    # Muting mid-sentence looked like it worked and then the transcript
    # arrived and was answered, because VAD closes an utterance a few hundred
    # milliseconds after the last word and a hand takes longer than that.
    voice_epoch: int = 0
    # The wake decoder's per-utterance state. Running the gate on the
    # committed buffer means nothing knows whether an utterance woke the
    # assistant until the operator stops talking, so a minute of speech is
    # discarded a minute after the phrase that should have started it. The
    # decoder is a streaming one; these three let it decide while the
    # operator is still speaking.
    #
    # `wake_stream` is sherpa's per-utterance handle (0.33 ms to make).
    # `wake_fired` is the verdict so far. `wake_decidable` is the one that
    # matters for safety: False means no live decoder ever saw this audio —
    # still warming, model absent, load failed — and the commit must fail
    # OPEN rather than read a False `wake_fired` as "not the wake word".
    wake_stream: Any = None
    wake_fired: bool = False
    wake_decidable: bool = False
    # Voice-latency legs. The live copies live on ``TurnState``, per turn, for
    # the same reason as every other field migrated off this class — two turns
    # sharing one slot report each other's timings. These two are transitional
    # fallbacks only, in the sense the class docstring describes: nothing writes
    # them, and `_tts_state` reaches them solely when the emit helpers are
    # driven outside a turn.
    voice_turn_started_at: float | None = None
    voice_first_speakable_at: float | None = None
    # The one exception, and only briefly: end of speech happens before any
    # turn exists, so `_start_turn` parks it here immediately before spawning
    # the turn and `_run_turn` claims it into that turn's ``TurnState`` and
    # empties the slot. It rides with the payload through the queue rather than
    # sitting here, so a rejected or queued-behind payload leaves nothing for
    # another turn to pick up.
    voice_commit_at: float | None = None
    # Server-side voice-input state machine (idle → listening →
    # transcribing → idle). Owns the `voice_state` wire emissions for the
    # speech-in half; the speech-back half (RESPONDING / SPEAKING) is
    # downstream (orb `thinking` + frontend `speaking_back`). See
    # `tesseract/mirror/server/voice_loop.py`.
    voice_loop: VoiceLoop = field(default_factory=VoiceLoop)
    # the running chat turns' TurnStates, keyed by chat_id
    # (`""` for a legacy no-chat-id turn). Registered by `_run_turn` at turn
    # start, popped in its finally. Out-of-turn paths (chat switch, barge-in,
    # Stop, WS cleanup) reach each turn's per-turn TTS state through this map
    # since their tasks don't see the turn's ContextVar.
    turn_states_by_chat: dict[str, Any] = field(default_factory=dict)
    # Per-turn TTS state.
    # MIGRATED to `TurnState` (turn_context.py); these
    # session fields remain as transitional fallbacks for direct-call test
    # paths (same contract as the Codex-fix M1 fields above). New code reads
    # `tts._tts_state(session)`.
    # `tts_buffer`: pending text accumulated from `stream_text` deltas, drained
    # at every sentence boundary. `tts_sequence`: monotonic per turn so the
    # frontend can detect drops and play in order. Reset at every turn start.
    tts_buffer: str = ""
    # The surface kind that OPENED the current `tts_buffer` segment — pinned
    # when the buffer was empty and deliberately not overwritten while it
    # fills, so a long `<intent>` spanning several deltas keeps its own
    # voicing. Carried so the end-of-turn flush in `_flush_tts_terminator`
    # synthesizes the tail with that preset rather than whichever surface
    # happened to arrive last. Note the buffer can therefore hold text from
    # more than one surface when the opening one never hit a sentence
    # boundary. Reset at turn start.
    tts_buffer_kind: str = "answer"
    # Latched by the first `<spoken>` delta of the turn; mutes every later
    # `<answer>` delta from TTS while it still streams to screen. Reset at
    # turn start.
    tts_spoken_seen: bool = False
    tts_sequence: int = 0
    # TTS synthesis is fired off as a chained background task so chat
    # stream deltas keep flowing while synthesis is in flight (each
    # sentence is ~200-600 ms). Each new task awaits the prior task
    # before synthesizing, preserving on-the-wire envelope order.
    # `_flush_tts_terminator` awaits this chain before emitting the
    # is_final=True chunk so playback never cuts mid-sentence.
    tts_synth_task: asyncio.Task[None] | None = None
    # Per-turn guard so a provider outage does not spam one toast per
    # sentence/chunk. Reset at turn start.
    tts_failure_notified: bool = False
    # Per-SESSION, unlike the guard above, and deliberately: a lane falling
    # through to the one behind it is a standing condition rather than a
    # per-turn event — the chosen voice stays unavailable until the operator
    # reloads it. Announced once and then carried by the HUD's lane marker,
    # because a toast on every turn is how a warning stops being read.
    tts_fallback_notified: bool = False
    # Operator-selected voice mode — the HUD pill cycles it and pushes the
    # value via the `voice_mode_set` envelope. The legal values and which
    # of them are silent live in `voice_modes.py`, shared with the handler
    # that validates them and the TTS path that gates on them. Defaults to
    # `transcribe` so a fresh session is silent until the operator opts in
    # — the assistant speaking unsolicited is the user's #1 friction.
    voice_mode: str = "transcribe"
    # conversation-layer Task 4.2 (Q2) — per-chat FIFO queue of typed/voice
    # payloads that arrive while a turn is already running for that chat.
    # Drained one entry at a time by `_run_turn`'s tail (`turn_intake.
    # drain_next`) as each queued entry's own turn completes — a queue
    # entry is a NORMAL turn, not a mid-turn inject. Was a single-slot
    # last-wins dict (`pending_user_payloads`); FIFO means no follow-up is
    # silently dropped by a later one — overflow past
    # `runtime.yaml::chat_queue_max` is rejected loudly instead (see
    # `turn_intake._start_turn`). Task 4.5 retired the single-slot
    # `pending_user_payload` back-compat property — callers use this dict
    # directly (or the `pending_user_text` convenience view below).
    chat_queues: dict[str, deque[dict[str, Any]]] = field(default_factory=dict)
    # Codex audit 2026-05-06 M1: workspace synthetic turns get their own
    # FIFO queue so a comment/operator_post arriving mid-turn cannot
    # overwrite a queued operator chat/voice payload. Multiple workspace
    # arrivals queue in arrival order — they're independent threads, no
    # last-wins coalescing. Capped at 64 to bound memory if the operator's
    # WS is wedged for a long time. Queue ownership is
    # `_drain_same_event_queue` in ws.py, not the chat-lane tail-drain, so
    # synthetic-turn completions are the sole driver. Overflow on append
    # routes through
    # `_enqueue_workspace_payload` which fires `cleared` on the evicted
    # head so its indicator doesn't hang.
    pending_workspace_payloads: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=64),
    )
    # Cost UX overhaul (2026-04-27): `cost_overage_ask` envelopes await
    # operator Yes/No on `cost_overage_response`. Same shape as the
    # tool-ask plumbing — futures keyed by `call_id`. Cleared on WS
    # close. A `None`/timeout response is treated as deny so an
    # unattended operator never silently approves overage spend.
    pending_overage_asks: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    # Carry across deltas for the structured-tag stream parser. The carry
    # holds a partial `<intent>`/`</answer>` token sitting at the boundary
    # between two stream chunks; the state names which surface (if any) we
    # are currently inside.
    stream_status_buffer: str = ""
    stream_tag_state: str = "outside"  # "outside" | "intent" | "spoken" | "answer"
    # True once we have logged an "untagged text" warning for this turn —
    # one warning per turn, not one per delta. Reset at turn start.
    stream_untagged_warned: bool = False
    # Workspace-redesign B (2026-05-06): when set, the current `_run_turn`
    # is a synthetic workspace turn fired in response to an operator
    # comment. `_handle_chunk` reads this flag to:
    #   1) suppress `stream_text` envelopes (chat view stays silent;
    #      reply is delivered via the workspace_reply tool, which renders
    #      under the operator's comment in the workspace thread).
    #   2) tag `loop_start` / `loop_end` with `workspace_origin` so the
    #      frontend dispatch routes the synthetic turn around the chat
    #      conversation store.
    # Cleared in `_run_turn`'s finally block.
    workspace_origin: dict[str, str] | None = None
    # Codex audit 2026-05-06 M2: armed by `_handle_chunk` when the assistant's
    # `workspace_reply` returns is_error=False during the current
    # `_run_turn`. The finally block reads this to decide whether to
    # confirm or rollback the deferred workspace delivery flags.
    workspace_reply_succeeded: bool = False
    # Ambient observer: per-turn view-context snapshot the Mirror
    # ships on `chat_message` envelopes. `_run_turn` consumes and clears
    # before each call to `chat_session.send` so the snapshot only
    # influences the turn it was captured for.
    pending_view_snapshot: dict[str, Any] | None = None
    # Serialize WebSocket writes. aiohttp's `ws.send_json` is not
    # safe for concurrent coroutines; without this lock, two parallel
    # turns emitting envelopes can interleave frame bytes. Held only
    # for the duration of one send — sub-millisecond on healthy WS.
    ws_send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # In-flight synthetic workspace turns keyed by event_id. Each
    # value is the asyncio.Task running an ephemeral forked ChatSession
    # (see ChatSession.fork_for_synthetic). Independent of
    # `current_turn_task` so chat + synthetic turns run concurrently;
    # cap enforced via `runtime.yaml::max_concurrent_synthetic_turns`.
    # Same-event-id arrivals queue into `pending_workspace_payloads`
    # to preserve thread ordering.
    synthetic_turn_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    # Spawn push Stage 2 (idle-wake) — chat_ids with a wake turn scheduled (or
    # in flight) for a finished background spawn. Set synchronously in the
    # spawn done-callback BEFORE the wake task is spawned, cleared when the wake
    # turn starts running, so a burst of completions schedules exactly one wake
    # per chat. See `tesseract/mirror/server/spawn_wake.py`.
    spawn_wake_pending: set[str] = field(default_factory=set)
    # mirror-multi-chat P1 — chat registry. ``chat_session`` above is the
    # ACTIVE chat; it MUST only be reassigned via ``switch_chat`` (which keeps
    # it in lock-step with ``active_chat_id``) — a bare ``session.chat_session =``
    # would silently drift it from ``chats[active_chat_id]``. ``chats`` holds
    # every open + archived ChatSession keyed by chat_id; ``chat_meta`` carries
    # the sidebar/persistence facts; ``chat_order`` is the insertion-ordered
    # list of NON-archived chat_ids (sidebar order). ``chats`` and ``chat_meta``
    # are always co-populated (by ``__post_init__`` and ``create_chat`` — the
    # only two registry-population paths). Seeded from the single
    # ``chat_session`` in ``__post_init__`` so a session built the old way is
    # byte-identical (one chat).
    chats: dict[str, ChatSession] = field(default_factory=dict)
    chat_meta: dict[str, ChatMeta] = field(default_factory=dict)
    active_chat_id: str = ""
    chat_order: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Back-compat seed: construction still passes one ``chat_session``.
        # Promote it to chat 1 unless the caller already populated ``chats``
        # (a future multi-chat load path may build the registry directly).
        if not self.chats:
            now = datetime.now().astimezone()
            cid = self.active_chat_id or uuid.uuid4().hex
            self.chats = {cid: self.chat_session}
            stamp_chat_id(self.chat_session, cid)
            self.chat_meta = {cid: _new_chat_meta(cid, now)}
            self.active_chat_id = cid
            self.chat_order = [cid]

    def create_chat(
        self,
        chat_session: ChatSession,
        *,
        title: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Register a new chat (does NOT switch to it). Returns its chat_id.

        Enforces the open-chat cap (D5): if registering pushes the open count
        past ``MAX_OPEN_CHATS``, the oldest open chat auto-archives.
        """
        now = now or datetime.now().astimezone()
        cid = uuid.uuid4().hex
        self.chats[cid] = chat_session
        stamp_chat_id(chat_session, cid)
        self.chat_meta[cid] = _new_chat_meta(cid, now, title=title)
        self.chat_order.append(cid)
        self.enforce_open_cap()
        return cid

    def evict_chat(self, chat_id: str) -> None:
        """Drop a chat from the OPEN SET without archiving it.

        The cap is this connection's memory window, not a filing decision. It
        used to be enforced with ``archive_chat``, which set ``archived`` on the
        victim's meta, so opening an eleventh conversation told the operator
        they had archived one they never touched, and the autosave wrote that
        claim to disk.

        The ChatSession stays in ``chats``, exactly as ``archive_chat`` leaves
        it, so switching back costs no rebuild. What leaves is the order, which
        is what the cap is about.
        """
        if chat_id in self.chat_order:
            self.chat_order.remove(chat_id)

    def enforce_open_cap(self) -> list[str]:
        """Trim the open set to ``MAX_OPEN_CHATS``, oldest first. Returns the
        ids that left, so a caller can tell the surface which ones went."""
        evicted: list[str] = []
        while len(self.chat_order) > MAX_OPEN_CHATS:
            victim = self.chat_order[0]
            self.evict_chat(victim)
            evicted.append(victim)
        return evicted

    def switch_chat(self, chat_id: str) -> None:
        """Make ``chat_id`` the active chat. Raises ``KeyError`` if unknown.

        An ARCHIVED chat is refused: ``archive_chat`` leaves the session in
        ``chats`` for the restore window, so without this guard a second switch
        would repoint the active chat at something flagged archived and absent
        from the order, and turns would run into it. `chat.restore` is the way
        back, and it un-archives deliberately.
        """
        if chat_id not in self.chats:
            raise KeyError(chat_id)
        # `.get`, not `[...]`: the two maps are kept in step in production but a
        # caller holding only a ChatSession may never have made a meta, and a
        # missing one is not an archived one.
        meta = self.chat_meta.get(chat_id)
        if meta is not None and meta.archived:
            raise ValueError(f"{chat_id} is archived")
        # A chat evicted by the cap is still here and still un-archived;
        # switching to it puts it back in the order rather than leaving the
        # active chat outside the set it belongs to.
        if chat_id not in self.chat_order:
            self.chat_order.append(chat_id)
            self.enforce_open_cap()
        self.active_chat_id = chat_id
        self.chat_session = self.chats[chat_id]

    def archive_chat(self, chat_id: str) -> None:
        """Soft-archive a chat (D1): mark archived, drop from sidebar order.

        The ChatSession stays in ``chats`` for the restore window. Archiving
        the active chat switches to the most-recent remaining open chat;
        archiving the only open chat raises ``ValueError``.
        """
        if chat_id not in self.chats:
            raise KeyError(chat_id)
        remaining = [c for c in self.chat_order if c != chat_id]
        if not remaining:
            raise ValueError("cannot archive the only open chat")
        self.chat_meta[chat_id].archived = True
        if chat_id in self.chat_order:
            self.chat_order.remove(chat_id)
        if chat_id == self.active_chat_id:
            self.switch_chat(self.chat_order[-1])

    def reopen_chat(
        self,
        chat_id: str,
        *,
        chat_session: ChatSession | None = None,
        meta: "ChatMeta | None" = None,
    ) -> None:
        """Restore an archived chat into the open set (P5) and focus it.

        A chat archived THIS session is still in ``chats`` — un-archive its meta
        and re-add it to ``chat_order``. A chat archived in a PRIOR session is gone
        from the live registry; the caller rebuilds it from disk and passes
        ``chat_session`` + ``meta``.

        ``chat.switch`` uses the rebuilt form too, for a conversation that was
        never archived and simply fell outside this connection's memory window.
        Nothing is un-archived in that case: the meta it passes already says
        so, and the archived branch above is not taken. Enforces the open cap (D5, oldest auto-archives)
        and switches active to the restored chat. Raises ``KeyError`` if the chat is
        absent from memory and no rebuilt session was supplied."""
        if chat_id in self.chats:
            self.chat_meta[chat_id].archived = False
        elif chat_session is not None and meta is not None:
            self.chats[chat_id] = chat_session
            stamp_chat_id(chat_session, chat_id)
            self.chat_meta[chat_id] = meta
        else:
            raise KeyError(chat_id)
        if chat_id not in self.chat_order:
            self.chat_order.append(chat_id)
        self.enforce_open_cap()
        self.switch_chat(chat_id)

    @property
    def current_turn_task(self) -> "asyncio.Task[None] | None":
        """The active chat's in-flight turn task (back-compat accessor)."""
        return self.current_turn_tasks.get(self.active_chat_id)

    @current_turn_task.setter
    def current_turn_task(self, task: "asyncio.Task[None] | None") -> None:
        cid = self.active_chat_id
        if task is None:
            self.current_turn_tasks.pop(cid, None)
        else:
            self.current_turn_tasks[cid] = task

    def release_turn_slot(
        self, chat_id: "str | None", task: "asyncio.Task[None] | None",
    ) -> bool:
        """Free `chat_id`'s turn slot, but only if `task` still holds it.

        A stopped turn keeps its slot until it has actually unwound, and this
        is what releases it. The guard matters because the slot may belong to
        a different turn by then: a drained follow-up can claim it while this
        one is still finishing. An unconditional pop would untrack the LIVE
        turn, and every busy check and every later stop reads that slot.
        Returns whether anything was released.
        """
        cid = self.active_chat_id if chat_id is None else chat_id
        if task is not None and self.current_turn_tasks.get(cid) is not task:
            return False
        self.current_turn_tasks.pop(cid, None)
        return True

    def has_running_turn(self) -> bool:
        """True if ANY chat (active or background) has an in-flight turn."""
        return any(t is not None and not t.done() for t in self.current_turn_tasks.values())

    @property
    def pending_user_text(self) -> str | None:
        """Back-compat convenience view over the active chat's queue TAIL text."""
        queue = self.chat_queues.get(self.active_chat_id)
        if not queue:
            return None
        text = queue[-1].get("text")
        return text if isinstance(text, str) else None

    @pending_user_text.setter
    def pending_user_text(self, value: str | None) -> None:
        # `None` clears the WHOLE queue for the active chat (matches the old
        # single-slot "nothing pending" meaning).
        cid = self.active_chat_id
        if value is None:
            self.chat_queues.pop(cid, None)
        else:
            self.chat_queues[cid] = deque([{"text": value, "attachments": []}])


async def send_envelope(session: ServerSession, envelope: dict[str, Any] | None) -> None:
    """Append to the session event log and forward to the WebSocket.

    A `None` envelope is a no-op (adapter-internal chunks like REASONING_ITEM
    have no UI surface). A closed WS swallows the send silently — the event
    log still captures the envelope for audit/replay.

    Holds `session.ws_send_lock` across the WS write so two parallel turns
    (chat + synthetic) can't interleave frame bytes. A test stub that does
    not declare the lock attribute falls back to a direct send (tests are
    single-threaded, no contention).
    """
    if envelope is None:
        return
    session.event_log.append(envelope)
    if session.ws.closed:
        return
    lock = getattr(session, "ws_send_lock", None)
    if lock is None:
        try:
            await session.ws.send_json(envelope)
        except ConnectionResetError:
            log.debug("ws closed mid-send for %s", session.session_id)
        return
    async with lock:
        try:
            await session.ws.send_json(envelope)
        except ConnectionResetError:
            log.debug("ws closed mid-send for %s", session.session_id)
