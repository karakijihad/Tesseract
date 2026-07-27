"""App-level spawn ownership index — M4-p2 (audit follow-up to Task 3.1).

Task 3.1 (`chat_restore.py`'s `mark_vanished_spawns` + `spawn_journal.
sweep_orphans`) stopped a same-process reconnect from falsely declaring a
still-running spawn "lost". This closes the other half of the gap: even
when a spawn survives reconnect, its owning `SpawnRegistry.
completion_notifier` is a closure pinned to the OLD session/chat objects
(`spawn_wake.wire_chat` captures `session`/`cs` by closure at wire time) —
a completion firing after reconnect notifies a dead `ChatSession` nobody
reads, and a spawn that finished DURING the disconnected window notifies it
and is never seen at all.

`SpawnOwnershipIndex` (at ``app["spawn_ownership"]``, seeded once like
``app.setdefault("parked_asks", {})`` in `session_factory.
create_server_session`) tracks which chat started which spawn and where
its completion currently belongs:

  - ``chat_handles``: chat_id -> [handle_id, ...] not yet finally delivered.
  - ``bindings``: handle_id -> `ChatBinding(session, chat_session, chat_id,
    registry)` — the CURRENT target a completion should land in, plus the
    (fixed, never-changing) `SpawnRegistry` that actually owns the handle.

``register_owner`` is wired into the spawn-start path — `spawn_wake.
wire_chat` sets it as `SpawnRegistry.on_register`, fired once per
`SpawnRegistry.register()` call (the process-global-handle-index sibling of
`completion_notifier`).

``rebind_chat`` is called from `chat_restore._restore_persisted_chats`,
AFTER Task 3.1's `mark_vanished_spawns` sweep, once per restored chat. For
every handle_id still tracked under that chat_id:

  - still running -> point its OWNING registry's `completion_notifier` at
    THIS reconnect's (session, chat_session) and move the binding here, so
    a completion firing later reaches the live chat.
  - terminal, not yet delivered -> replay its completion into THIS chat now
    (the dead-window case: its own notifier already fired into an orphaned
    `ChatSession` nobody reads).
  - cancelled, or the handle has vanished entirely (weakref dropped) ->
    drop the bookkeeping; nothing to notify (mirrors `SpawnRegistry.
    _on_done`'s own skip-cancelled rule — a cancelled spawn never surfaces
    a completion note).

Hygiene (mirrors the `parked_asks` pattern of popping settled entries, see
`ask_gate.py`): a handle's bookkeeping is dropped the moment its completion
is delivered to a chat whose session is CURRENTLY CONNECTED (``session_id``
present in ``app["server_sessions"]`` — the same liveness check
`session_cleanup.cleanup_session` and every `routes/*.py` session lookup
uses). A completion delivered while the owning session is already
disconnected (the dead-window race) is NOT finalized — it stays tracked so
the next reconnect's `rebind_chat` replays it into a session that is
actually read. This is what avoids both failure modes: losing a
dead-window completion, and re-replaying an already-observed one on some
much later reconnect.

Purely synchronous bookkeeping throughout (dict lookups, attribute
assignment, at most one deque append per terminal replay) — no I/O, no
awaits, so `rebind_chat`'s per-handle loop needs no `asyncio.gather`
fan-out even though it can visit N handles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatBinding:
    """The (session, chat_session, chat_id) a spawn's completion currently
    belongs to, plus the ``registry`` that actually OWNS the handle (the
    `SpawnRegistry` whose task done-callback closure was captured at
    `SpawnRegistry.register()` time).

    ``registry`` is fixed for the handle's whole lifetime — after a rebind,
    ``chat_session`` moves forward to the new chat, but the object that will
    actually fire the completion never changes (it's the ORIGINAL chat's
    registry; `chat_session.spawns` on the NEW chat is a different, empty
    registry). Every rebind must carry ``registry`` forward unchanged, never
    re-derive it from the (now-current) ``chat_session``."""

    session: Any
    chat_session: Any
    chat_id: str
    registry: Any


class SpawnOwnershipIndex:
    """Process-lifetime index; see module docstring for the field shapes
    and the delivery/finalize contract."""

    def __init__(self) -> None:
        self.chat_handles: dict[str, list[str]] = {}
        self.bindings: dict[str, ChatBinding] = {}
        self._delivered: set[str] = set()

    def register(self, chat_id: str, handle_id: str, binding: ChatBinding) -> None:
        self.chat_handles.setdefault(chat_id, []).append(handle_id)
        self.bindings[handle_id] = binding

    def mark_delivered(self, handle_id: str) -> bool:
        """True the first time this handle_id is marked delivered; False on
        any repeat call — the dedup guard against a rebind-time replay
        racing the original (stale) notifier's own firing."""
        if handle_id in self._delivered:
            return False
        self._delivered.add(handle_id)
        return True

    def finalize(self, chat_id: str, handle_id: str) -> None:
        """Drop a handle's bookkeeping once its completion has been finally
        delivered to a live chat (or it's cancelled/vanished — nothing left
        to deliver). Idempotent — safe to call more than once."""
        self.bindings.pop(handle_id, None)
        self._delivered.discard(handle_id)
        handles = self.chat_handles.get(chat_id)
        if handles is None:
            return
        if handle_id in handles:
            handles.remove(handle_id)
        if not handles:
            self.chat_handles.pop(chat_id, None)


def get_ownership_index(app: "web.Application | None") -> "SpawnOwnershipIndex | None":
    """``None`` for non-Mirror callers (REPL / bare unit tests that wire
    `spawn_wake` without a real app) — ownership tracking is Mirror-only."""
    if app is None:
        return None
    return app.setdefault("spawn_ownership", SpawnOwnershipIndex())


def register_owner(
    app: "web.Application | None",
    session: Any,
    chat_session: Any,
    chat_id: str,
    handle_id: str,
) -> None:
    """Record that ``handle_id`` started under ``chat_id``, currently owned
    by ``(session, chat_session)``. Wired via `spawn_wake.wire_chat` onto
    `SpawnRegistry.on_register` — ``chat_session.spawns`` at THIS moment is,
    by construction, the registry that actually owns the handle."""
    index = get_ownership_index(app)
    if index is None:
        return
    index.register(
        chat_id, handle_id,
        ChatBinding(session, chat_session, chat_id, chat_session.spawns),
    )


def _is_connected(app: "web.Application", session: Any) -> bool:
    session_id = getattr(session, "session_id", None)
    if session_id is None:
        return False
    return (app.get("server_sessions") or {}).get(session_id) is not None


def deliver_once(
    app: "web.Application | None",
    session: Any,
    chat_id: str,
    chat_session: Any,
    handle: Any,
) -> None:
    """Fold ``handle``'s completion into ``chat_session`` (the notifier's
    floor, and `rebind_chat`'s dead-window replay).

    ``app`` absent (non-Mirror caller, e.g. the bare-registry unit tests in
    `test_spawn_idle_wake.py`) -> deliver unconditionally, no bookkeeping —
    byte-identical to pre-M4-p2 behavior.

    ``app`` present, ``session`` still CONNECTED -> this is (or replays) a
    real, observed delivery: dedup-guarded (`mark_delivered`, defends
    against a hypothetical double-dispatch onto the same live target) and
    finalized — no further bookkeeping is needed for a handle already
    delivered live.

    ``app`` present, ``session`` already disconnected -> a dead-window note:
    deliver anyway (the notifier fires exactly once per spawn; something
    must observe it even though this destination is about to be orphaned),
    but do NOT mark it delivered or finalize — a delivery nobody live reads
    must not block the NEXT reconnect's `rebind_chat` from replaying it into
    a chat that is.
    """
    if app is None:
        chat_session.ingest_spawn_completion(handle)
        return
    index = get_ownership_index(app)
    if index is not None and _is_connected(app, session):
        if index.mark_delivered(handle.handle_id):
            chat_session.ingest_spawn_completion(handle)
        index.finalize(chat_id, handle.handle_id)
    else:
        chat_session.ingest_spawn_completion(handle)


def rebind_chat(
    app: "web.Application", session: Any, chat_session: Any, chat_id: str,
) -> None:
    """Reconnect-time re-association — see module docstring. Called from
    `chat_restore._restore_persisted_chats` after Task 3.1's
    `mark_vanished_spawns` sweep has already dropped genuinely-dead spawns;
    only handles `find_handle` still vouches for reach here.
    """
    from tesseract.brain.spawns import find_handle
    from tesseract.mirror.server.spawn_wake import _build_notifier

    index = get_ownership_index(app)
    if index is None:
        return
    for handle_id in list(index.chat_handles.get(chat_id, ())):
        handle = find_handle(handle_id)
        if handle is None:
            index.finalize(chat_id, handle_id)  # vanished — nothing to rebind
            continue
        if handle.is_running():
            old_binding = index.bindings.get(handle_id)
            registry = old_binding.registry if old_binding is not None else None
            if registry is not None:
                # Mutate the ORIGINAL owning registry's notifier slot — never
                # `chat_session.spawns` (the new chat's own, unrelated,
                # registry) — so the still-pending done-callback (bound to
                # `registry` forever since `register()` time) calls forward.
                registry.completion_notifier = _build_notifier(
                    app, session, chat_id, chat_session,
                )
            index.bindings[handle_id] = ChatBinding(session, chat_session, chat_id, registry)
            continue
        if handle.cancelled or handle.task.cancelled():
            index.finalize(chat_id, handle_id)  # matches _on_done's skip-cancelled rule
            continue
        # Terminal, not yet delivered to a connected session — the
        # dead-window case: fold it into the chat that's actually live now.
        deliver_once(app, session, chat_id, chat_session, handle)


__all__ = [
    "ChatBinding",
    "SpawnOwnershipIndex",
    "get_ownership_index",
    "register_owner",
    "deliver_once",
    "rebind_chat",
]
