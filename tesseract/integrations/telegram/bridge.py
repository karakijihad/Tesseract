from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.integrations._channel_adapter import (
    ChannelMessage,
    ChannelStatus,
    ChannelUser,
    ChannelUserTier,
)
from tesseract.integrations._channel_attachment import (
    ChannelAttachment,
    render_envelope,
)
from tesseract.integrations._channel_gate import (
    _PER_TURN_ATTR,
    PendingAsks,
    build_channel_ask_fn,
    cancel_chat_asks,
    reset_per_turn_state,
    resolve_channel_ask,
)
from tesseract.integrations._channel_progress import (
    ProgressEvent,
    ProgressThrottler,
    format_progress_line,
)
from tesseract.integrations._channel_uploads import (
    StoredChannelAttachment,
    save_channel_attachment,
)
from tesseract.integrations._channels_config import (
    ChannelsConfig,
    GatePolicy,
    channel_key_env,
)
from tesseract.integrations._chat_memory import ChatMemoryService
from tesseract.integrations._conversation_store import ConversationStore
from tesseract.integrations._url_extract import (
    extract_urls_to_context,
    find_urls,
)
from tesseract.integrations._handlers.document import (
    DocumentHandlerError,
    extract_document_text,
)
from tesseract.integrations._handlers.image import ImageHandlerError, describe_image
from tesseract.integrations._handlers.voice import (
    VoiceHandlerError,
    transcribe_voice_audio,
)
from tesseract.integrations._channel_session import (
    compact_after_turn,
    is_new_local_day,
    offer_a_fresh_session,
)
from tesseract.integrations.telegram.api import (
    TelegramAPI,
    TelegramAPIError,
    TelegramMessage,
    parse_message_update,
)
from tesseract.integrations.telegram.chunker import (
    TELEGRAM_TEXT_MAX,
    chunk_for_telegram,
)
from tesseract.integrations.telegram.download import (
    FetchRejection,
    fetch_telegram_attachment,
)
from tesseract.integrations.telegram.commands import (
    TelegramCommandContext,
    dispatch as dispatch_command,
    is_known_command,
)
from tesseract.integrations.telegram.format import markdown_to_telegram_html
from tesseract.integrations.telegram.state import (
    OfflineMessage,
    PendingChat,
    StateBundle,
    load_allowlist,
    load_status,
    save_allowlist,
    save_state,
    save_status,
)
from tesseract.mirror.server.event_log import EventLog
from tesseract.mirror.server import spawn_wake
from tesseract.mirror.server.session import ServerSession, _build_chat_session

log = logging.getLogger(__name__)

_POLL_TIMEOUT_SECONDS = 25
_REPLY_TICK_SECONDS = 1.0
_BACKOFF_INITIAL_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0
# Watchdog backoff for ``getMe`` failures (bridge "starting" state).
# Caps at 60s so a long outage (DNS / Telegram API down) doesn't burn
# request budget, while a 1s blip still recovers on the next tick.
_GETME_BACKOFF_INITIAL_SECONDS = 1.0
_GETME_BACKOFF_MAX_SECONDS = 60.0
_TELEGRAM_TEXT_MAX = TELEGRAM_TEXT_MAX


class _NullWebSocket:
    closed = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        del payload
        return None


async def _deny_ask(*args, **kwargs) -> bool:
    del args, kwargs
    return False


async def _noop_cli_sink(*args, **kwargs) -> None:
    del args, kwargs
    return None


async def _deny_overage(*args, **kwargs) -> bool:
    del args, kwargs
    return False


async def _noop_status_emit(*args, **kwargs) -> None:
    del args, kwargs
    return None


#: Inbound kinds a decoder reads, and what reading one produces. The channel
#: document renders this: the assistant was told for weeks that it could not
#: transcribe voice, in an apology example written by hand, while
#: `_decode_voice` had been transcribing it all along. Prose about what the
#: runtime can do belongs to the runtime.
DECODED_KINDS: dict[str, str] = {
    "voice": "transcribed",
    "photo": "described",
    "document": "text-extracted",
}

#: Kinds with no extractor. They are still fetched and stored, so a later turn
#: can reference the file ("edit the GIF you sent yesterday"); they keep
#: `no_handler` because no text came out of them.
PERSISTED_KINDS: tuple[str, ...] = ("audio", "video", "video_note", "animation", "sticker")


class TelegramBridge:
    """Telegram long-polling bridge — first concrete :class:`ChannelAdapter`.

    Inbound messages are appended to the per-channel conversation store
    (``logs/channels/telegram/<chat_id>/conversations.jsonl``) and drive
    a dedicated chat turn via :func:`_start_channel_turn` — they do NOT
    write to ``workspace_events``. Each chat keeps its own sliding-window
    history (per :class:`RetentionPolicy`); a long inactivity gap rebuilds
    the chat session so the next message starts fresh.
    """

    name = "telegram"

    def __init__(
        self,
        *,
        token: str,
        app: web.Application,
        conversation_store: ConversationStore,
        env_seed_chat_ids: str | None = None,
        chat_memory: ChatMemoryService | None = None,
    ) -> None:
        self._token = token
        self._app = app
        self._state = StateBundle(env_seed=env_seed_chat_ids)
        self._api: TelegramAPI | None = None
        # Owns the per-chat rolling summary and auto-recall on inbound. The
        # end-of-conversation recap is not its job any more — every entry point
        # earns the same one, written by the capture funnel. ``None`` is legal
        # (minimal fixtures); the bridge no-ops cleanly when missing.
        self._chat_memory = chat_memory
        # One ServerSession per chat_id — each remote user gets isolated
        # chat history and a private inactivity-reset clock. Operator's
        # Mirror sessions live in `app["server_sessions"]`; Telegram
        # sessions are deliberately separate (headless, no WS) so they
        # do not show up in the cockpit session list.
        self._sessions: dict[int, ServerSession] = {}
        self._conversations = conversation_store
        self._poll_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # Per-chat serialization for parallel inbound handling. The poll
        # loop spawns a handler task per message (so a 30s delegate on
        # chat A no longer freezes getUpdates and prevents chat B from
        # being seen), but two rapid inbounds on the SAME chat must still
        # run serially — otherwise the chat_brain history, attachment
        # decode, and outbound order would race. One lock per chat_id.
        # Operator stance (2026-05-17): "no thread blocks".
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._inflight_handlers: set[asyncio.Task[None]] = set()
        # 2026-05-17 — ASK → Telegram round-trip. When the channel gate
        # fires, the bridge sends an inline-keyboard prompt to the
        # operator's chat. We stash ``event_id → {chat_id, message_id,
        # tool_name}`` so the callback handler can edit the right message.
        # In-memory only, and deliberately so: the turn that a prompt is
        # attached to lives in this process, so a restart ends both together
        # and a tap afterwards is honestly reported as too late.
        self._pending_approval_messages: dict[str, dict[str, Any]] = {}
        # ``event_id -> PendingAsk`` for gated calls whose turn is parked
        # waiting on an answer. Shared with the Mirror inbox route (which
        # reaches it through ``app['telegram_bridge']``) so a tap on either
        # surface resolves the one future the turn is awaiting.
        self._pending_channel_asks: PendingAsks = {}
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._last_poll_at: str | None = None
        self._error_count = 0
        # Self-heal state: ``starting`` means the supervised task is alive
        # and retrying ``getMe`` in the background. Operator-visible via
        # ``status_snapshot.bridge_state`` so the Channels tab shows
        # "starting" instead of a deceptive "stopped" while we're
        # waiting for the network to come back.
        self._bridge_phase: str = "stopped"
        self._last_getme_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Boot the bridge with a supervised auto-heal loop.

        Pre-2026-05-16, three failed ``getMe`` attempts at boot left the
        bridge permanently disabled for the process lifetime — the
        operator had to hit Restart in Mirror once the network came
        back. Now ``start()`` returns immediately after launching a
        supervised task that retries ``getMe`` indefinitely with bounded
        exponential backoff (1 → 60s cap). When ``getMe`` finally
        succeeds, the task transitions into the normal ``_poll_loop``
        and the bridge starts pulling updates. The Channels tab status
        flips from ``stopped`` → ``starting`` → ``running`` so the
        operator can see what state the bridge is in at a glance.
        """
        self._api = TelegramAPI(self._token)
        self._bridge_phase = "starting"
        self._poll_task = asyncio.create_task(
            self._never_die_supervisor(), name="telegram:supervisor"
        )

    async def _never_die_supervisor(self) -> None:
        """Wrap ``_supervised_loop`` in a crash-recovery shell.

        ``_supervised_loop`` already retries ``getMe`` and ``getUpdates``
        on every kind of network error. The only way it can exit
        unexpectedly is an unhandled exception in our own code (e.g.
        a regression in ``_handle_message``). When that happens we log
        and respawn after a short pause rather than leaving the bridge
        silently dead — the operator wants self-healing, not a
        forensic exercise.
        """
        while not self._stop_event.is_set():
            try:
                await self._supervised_loop()
                return  # clean exit (stop() fired)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram: supervisor crashed; respawning in 5s")
                self._bridge_phase = "error"
                await self._sleep_or_stop(5.0)
                self._bridge_phase = "starting"

    async def _supervised_loop(self) -> None:
        """Wait until ``getMe`` succeeds, then run ``_poll_loop`` forever.

        ``_poll_loop`` swallows its own errors and never returns until
        ``stop()`` fires, so a ``getUpdates`` outage rides the
        ``_BACKOFF_MAX_SECONDS`` retry — no need for re-entry here. The
        only path that requires this supervisor is the boot-time
        ``getMe`` failure: TLS handshake / DNS / Telegram-API down at
        the moment the Mirror process launches. Pre-fix that left the
        bridge silently disabled until manual restart.
        """
        assert self._api is not None
        backoff = _GETME_BACKOFF_INITIAL_SECONDS
        me: dict[str, Any] | None = None
        attempt = 0
        while not self._stop_event.is_set() and me is None:
            attempt += 1
            try:
                me = await self._api.get_me()
                self._last_getme_error = None
            except TelegramAPIError as exc:
                exc_name = (
                    type(exc.__cause__).__name__
                    if exc.__cause__
                    else type(exc).__name__
                )
                self._last_getme_error = f"{exc_name}: {exc}"
                # Log loudly the first time, then quietly — long outages
                # shouldn't flood the err log every 60s. The first
                # failure tells the operator "look here"; subsequent
                # ones go to debug so a one-day outage doesn't bury
                # other warnings.
                if attempt == 1:
                    log.warning(
                        "telegram: getMe failed (%s); will retry every "
                        "%.1f→%.1fs until reachable",
                        self._last_getme_error,
                        _GETME_BACKOFF_INITIAL_SECONDS,
                        _GETME_BACKOFF_MAX_SECONDS,
                    )
                else:
                    log.debug(
                        "telegram: getMe attempt #%d still failing (%s)",
                        attempt, self._last_getme_error,
                    )
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2.0, _GETME_BACKOFF_MAX_SECONDS)
        if self._stop_event.is_set() or me is None:
            return
        username = me.get("username") if isinstance(me, dict) else None
        log.info(
            "telegram: bridge running (bot=@%s, allowlisted=%d, pending=%d, "
            "blocked=%d, getMe attempts=%d)",
            username,
            len(self._state.allowlist.chat_ids),
            len(self._state.allowlist.pending),
            len(self._state.allowlist.blocked),
            attempt,
        )
        self._bridge_phase = "running"
        # M1 — recovery drain. If the bridge restarted between a queue
        # of offline messages landing and the operator flipping back to
        # online, the inbox is non-empty *and* the current override is
        # online. Drain in a background task so the supervised loop
        # transitions into the poll loop promptly; replay failure is
        # logged, not surfaced to the bootstrapper.
        try:
            current = load_status(self._state.status_path)
            self._state.status = current
            if (
                current.override != "offline"
                and self._state.poll_state.offline_inbox_depth() > 0
            ):
                asyncio.create_task(
                    self.drain_offline_inbox(),
                    name="telegram:boot-drain",
                )
        except Exception:
            log.exception("telegram: boot-drain scheduling failed")
        await self._poll_loop()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("telegram: poll task shutdown failed")
        # Drain inbound handlers spawned by ``_spawn_handler``. Cap at 10s
        # so a wedged delegate can't block bridge restart. After the cap,
        # ``t.cancel()`` propagates ``CancelledError`` up the await chain
        # — for an in-flight ``delegate_coder`` this unwinds the brain
        # turn but does NOT actively kill the spawned ``claude`` CLI
        # subprocess (``race_communicate`` only kills on its internal
        # ``cancel_event``, which we don't set here). The subprocess
        # parents off the pre-restart process and gets cleaned up by the
        # OS when its stdout closes. Track-as-known-limitation: a wedged
        # CLI process can survive a Mirror restart for a few seconds.
        # ``getattr`` guards for test fixtures that bypass ``__init__``
        # (matches the ``_chat_memory`` pattern below).
        inflight = getattr(self, "_inflight_handlers", None)
        if inflight:
            pending = [t for t in inflight if not t.done()]
            if pending:
                log.info("telegram: draining %d in-flight handlers", len(pending))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "telegram: %d handlers still running after 10s — cancelling",
                        sum(1 for t in pending if not t.done()),
                    )
                    for t in pending:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            inflight.clear()
        chat_locks = getattr(self, "_chat_locks", None)
        if chat_locks is not None:
            chat_locks.clear()
        for session in list(self._sessions.values()):
            await self._cancel_session_turn(session)
        self._sessions.clear()
        if self._api is not None:
            try:
                await self._api.aclose()
            except Exception:
                log.exception("telegram: api close failed")
        self._api = None
        self._bridge_phase = "stopped"
        # Reset the event so a follow-up start() can run. The pre-fix
        # ``_stop_event.set()`` left it permanently set, which meant
        # ``_supervised_loop`` exited immediately on the next ``start()``
        # call — silently breaking the Restart button.
        self._stop_event = asyncio.Event()

    def _spawn_handler(self, message: TelegramMessage) -> None:
        """Background-task a single inbound so the poll loop stays responsive.

        Per-chat serialization is enforced inside ``_handle_message`` via
        ``_chat_locks[chat_id]``. The handler task is tracked in
        ``_inflight_handlers`` so it can't be garbage-collected mid-run
        and so ``stop()`` can drain it.
        """
        task = asyncio.create_task(
            self._handle_message_guarded(message),
            name=f"telegram:inbound:{message.chat_id}:{message.message_id}",
        )
        self._inflight_handlers.add(task)
        task.add_done_callback(self._inflight_handlers.discard)

    async def _poll_loop(self) -> None:
        assert self._api is not None
        backoff = _BACKOFF_INITIAL_SECONDS
        while not self._stop_event.is_set():
            try:
                offset = None
                if self._state.poll_state.last_update_id is not None:
                    offset = self._state.poll_state.last_update_id + 1
                updates = await self._api.get_updates(
                    offset=offset,
                    timeout=_POLL_TIMEOUT_SECONDS,
                    # Declared at the call site because this loop is what
                    # decides which kinds it can dispatch, and Telegram
                    # filters the rest SERVER-side: an update type missing
                    # here is never delivered and leaves no trace anywhere
                    # to notice it by. `callback_query` carries the approval
                    # keyboard's taps, and its absence meant every ✓ Approve
                    # since the button shipped was swallowed before it
                    # reached the bridge — while the handler's unit tests
                    # passed, because they call it directly.
                    allowed_updates=("message", "callback_query"),
                )
                self._last_poll_at = datetime.now(timezone.utc).isoformat()
                backoff = _BACKOFF_INITIAL_SECONDS
            except asyncio.CancelledError:
                raise
            except TelegramAPIError as exc:
                self._error_count += 1
                log.warning("telegram: getUpdates failed (%s); retrying in %.1fs", exc, backoff)
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2.0, _BACKOFF_MAX_SECONDS)
                continue
            except Exception:
                self._error_count += 1
                log.exception("telegram: poll loop crashed; retrying in %.1fs", backoff)
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2.0, _BACKOFF_MAX_SECONDS)
                continue

            for raw in updates:
                update_id = raw.get("update_id") if isinstance(raw, dict) else None
                if isinstance(update_id, int):
                    self._state.poll_state.last_update_id = update_id
                # 2026-05-17 — inline-keyboard callbacks from ASK prompts
                # arrive under `callback_query`, not `message`. Dispatch
                # them inline (they're cheap — one approval token write +
                # one editMessageText) rather than through the per-chat
                # task pool. Approval state must be applied BEFORE the
                # next inbound that hashes the same args, so synchronous
                # is the right choice.
                if isinstance(raw, dict) and isinstance(raw.get("callback_query"), dict):
                    try:
                        await self._handle_callback_query(raw["callback_query"])
                    except Exception:
                        log.exception("telegram: callback_query handler crashed")
                    continue
                message = parse_message_update(raw) if isinstance(raw, dict) else None
                if message is None:
                    # DIAGNOSTIC (MO-10-followup) — dump the top-level keys
                    # of any update parse-rejected so we can see whether
                    # voice messages arrive under `message` or under
                    # `edited_message`/`business_message`/some other kind.
                    if isinstance(raw, dict):
                        log.warning(
                            "telegram: parse_message_update returned None for update_id=%s keys=%s",
                            update_id, sorted(raw.keys()),
                        )
                        msg_node = raw.get("message") if isinstance(raw.get("message"), dict) else None
                        if msg_node is not None:
                            log.warning(
                                "telegram: parse-rejected message keys=%s chat=%s",
                                sorted(msg_node.keys()),
                                (msg_node.get("chat") or {}).get("type"),
                            )
                    continue
                self._spawn_handler(message)
            with self._state.with_lock():
                save_state(self._state.state_path, self._state.poll_state)

    async def _handle_message_guarded(self, message: TelegramMessage) -> None:
        """Acquire the per-chat lock then delegate to ``_handle_message``.

        Lock dict grows by one entry per distinct chat; that's bounded by
        the operator's allow-list. We do not prune entries because the
        lock object itself is cheap (asyncio.Lock is ~200 bytes).
        """
        lock = self._chat_locks.get(message.chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[message.chat_id] = lock
        # BEFORE the lock, never inside it. A turn parked on an approval
        # prompt holds this lock, so a new message that waited for the lock
        # first could never reach the code that releases it — the operator
        # would be talking to a bot that had gone deaf until the gate's
        # timeout expired. Sending another message means moving on from the
        # prompt, so the pending calls are refused and the turn completes.
        superseded = cancel_chat_asks(
            self._channel_asks(), str(message.chat_id),
        )
        for entry in superseded:
            log.info(
                "telegram: new message superseded pending ask %s (tool=%s)",
                entry.event_id, entry.tool_name,
            )
        async with lock:
            try:
                await self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "telegram: _handle_message crashed for chat=%s message_id=%s",
                    message.chat_id, message.message_id,
                )

    async def _handle_message(self, message: TelegramMessage) -> None:
        assert self._api is not None
        # Reject non-private chats outright. Groups/supergroups/channels
        # collapse identity to a single chat_id shared by every member,
        # so one approval would grant the whole room access.
        if message.chat_type != "private":
            log.info(
                "telegram: rejecting non-private chat_type=%s (chat_id=%s)",
                message.chat_type,
                message.chat_id,
            )
            return

        self._state.status = load_status(self._state.status_path)
        if self._state.status.override is None and not self._state.status_path.exists():
            save_status(self._state.status_path, self._state.status)

        with self._state.with_lock():
            self._state.allowlist = load_allowlist(
                self._state.allowlist_path,
                env_seed=os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"),
            )
            if self._state.allowlist.is_blocked(message.chat_id):
                # Blocked chats are silently dropped — no state mutation,
                # no reply, no pending record. Matches /ignore semantics
                # in IRC: the operator decided this chat no longer exists.
                return
            allowed = self._state.allowlist.is_allowed(message.chat_id)
            offline = self._state.status.override == "offline"
            first_time = False
            if not allowed:
                first_time = self._state.allowlist.record_pending(
                    message.chat_id,
                    message.from_username,
                )
                if first_time:
                    save_allowlist(self._state.allowlist_path, self._state.allowlist)
        if not allowed:
            if first_time:
                await self._safe_send(
                    chat_id=message.chat_id,
                    text=(
                        "This Telegram chat is waiting for operator approval. "
                        f"Chat id: {message.chat_id}."
                    ),
                )
            return

        # M3 — TTL expiry. An allowlisted chat with a per-user TTL in the
        # past is auto-revoked (moved to pending) and replied to once
        # with an explanation. The next message hits the pending flow.
        if self._ttl_expired(message.chat_id):
            await self._auto_revoke_for_ttl(message.chat_id)
            return

        chat_key = str(message.chat_id)
        tier = self._state.poll_state.user_tier.get(chat_key, "operator")

        # /clear follow-up (2026-05-16). When the previous turn left a
        # `pending_clear` stamp for this chat, the current message is
        # the operator's yes/no/anything-else answer — handled here
        # before the command router so a literal "yes" doesn't fall
        # through to a normal chat turn.
        if await self._handle_pending_clear_followup(message, chat_key, tier):
            return

        # AU-10 — agenda quick-reply (operator-tier only). Matches strings
        # shaped exactly like ``ag-YYYY-MM-DD-HHMM-<slug>:<verb>``; falls
        # through otherwise so casual operator chat stays conversational.
        text_stripped = (message.text or "").strip()
        if tier == "operator" and await self._handle_agenda_quick_reply(
            message, text_stripped
        ):
            return

        # m2 — deterministic command router. Read-only commands resolve
        # before the chat turn so the operator sees the same answer on
        # phone or cockpit, independent of chat_brain decisions.
        if text_stripped.startswith("/") and is_known_command(text_stripped):
            ctx = TelegramCommandContext(
                app=self._app,
                chat_id=message.chat_id,
                tier=tier,
                offline=offline,
                bridge=self,
            )
            reply = await dispatch_command(text_stripped, ctx)
            if reply is not None:
                # Command replies often carry HTML markup (``<b>Missions</b>``,
                # ``format_exec_summary`` headers). Routing through
                # ``send_text`` picks the chunker + HTML-first send + plain
                # fallback path — ``_safe_send`` ships text with no parse
                # mode so raw ``<b>`` tags would appear on the phone.
                await self.send_text(chat_ref=str(message.chat_id), text=reply)
                return

        # Visibility-first body (CR-1) + concrete decoders (CR-2). Each
        # ``no_handler`` attachment runs through :meth:`_decode_attachment`
        # which fetches bytes and dispatches on ``kind`` — promoting the
        # envelope to ``status="ready"`` with an ``<extracted>`` body, or
        # to ``too_large`` / ``extract_failed`` so the assistant gets a specific
        # signal instead of silently dropping the content. Unhandled
        # kinds keep ``no_handler``; CR-2 deliberately leaves video /
        # sticker / location / contact / poll / dice at that level so
        # The assistant can apologize or propose a tool.
        decoded = await self._decode_attachments(message.attachments, message)
        envelope = render_envelope(decoded)
        if envelope:
            if message.text:
                model_body = f"{message.text}\n\n{envelope}"
            else:
                model_body = envelope
        else:
            model_body = message.text

        now_iso = datetime.now(timezone.utc).isoformat()
        self._conversations.append(
            self.name,
            chat_key,
            ChannelMessage(
                ts=now_iso,
                direction="inbound",
                body=model_body,
                extra={
                    "telegram_message_id": message.message_id,
                    "from_user_id": message.from_user_id,
                    "from_username": message.from_username,
                    "telegram_date": message.date,
                    "has_attachments": bool(message.attachments),
                },
                attachments=decoded,
            ),
        )
        # Read BEFORE the write below overwrites it: the question is whether
        # the PREVIOUS message fell on an earlier day.
        crossed_into_a_new_day = is_new_local_day(
            self._state.poll_state.last_message_ts.get(chat_key)
        )
        with self._state.with_lock():
            self._state.poll_state.last_message_ts[chat_key] = now_iso
            self._state.poll_state.messages_in_total[chat_key] = (
                self._state.poll_state.messages_in_total.get(chat_key, 0) + 1
            )
            self._state.poll_state.first_seen.setdefault(chat_key, now_iso)
            self._state.poll_state.record_inbound(chat_key, now_iso)
            save_state(self._state.state_path, self._state.poll_state)

        if offline:
            # M1 — enqueue the message for replay on flip-to-online. The
            # honest wording matches the runtime behaviour now: the
            # message is *saved*, not just archived; the operator will
            # drain on resume.
            with self._state.with_lock():
                depth = self._state.poll_state.enqueue_offline(
                    chat_key,
                    OfflineMessage(
                        ts=now_iso,
                        telegram_message_id=message.message_id,
                        text=model_body,
                        from_user_id=message.from_user_id,
                        from_username=message.from_username,
                    ),
                )
                save_state(self._state.state_path, self._state.poll_state)
            tail = "" if depth == 1 else f" ({depth} queued)"
            await self._safe_send(
                chat_id=message.chat_id,
                text=(
                    "the assistant is offline right now. Your message is saved and "
                    f"will be processed when the assistant is back online{tail}."
                ),
            )
            return

        # Session 1 (2026-05-16) — the inactivity reset is gone. Long-idle
        # context survives via the rolling summary + auto-recall path
        # (see ``ChatMemoryService``); rotating the ChatSession would
        # discard the in-memory window for no win. Revoke/block still
        # rebuilds the session via :meth:`revoke` / :meth:`block`.
        session = self._session_for(message.chat_id, reset=False)
        # CR-5 — clear the per-turn gate-dedup set before the loop runs so
        # the first call to a previously gated tool can re-emit a fresh
        # ``agent_post`` if the operator is still away.
        reset_per_turn_state(session)

        # Session 3 (2026-05-16) — instant "saw it" reaction. Telegram
        # bots can react to messages with a single emoji via
        # ``setMessageReaction``. We place a 💭 on every inbound BEFORE
        # the turn runs so the operator gets a sub-second ack even when
        # the chat-action / placeholder paths are slow. Best-effort —
        # an API failure here just means no reaction; the turn proceeds
        # normally. Fires in the background so we don't add latency.
        asyncio.create_task(
            self._safe_react(message.chat_id, message.message_id, "💭"),
            name=f"telegram:react:{message.message_id}",
        )

        # Session 3 (2026-05-16) — typing-action keepalive. Telegram's
        # typing dot times out after ~5s, so a single firing at turn-start
        # makes long turns look dead. The background task re-fires every
        # 4s until cancelled so the assistant visibly "stays at the keyboard" for
        # multi-second tool runs. Cancellation happens on every exit
        # branch (success, exception, gated, no-reply) so we never leak
        # a typing task.
        typing_task = asyncio.create_task(
            self._typing_keepalive(message.chat_id),
            name=f"telegram:typing:{message.chat_id}",
        )

        # Placeholder-edit progress pattern: post a "thinking" bubble
        # immediately so the remote user gets an instant read-receipt,
        # then edit it to the final reply when the turn lands. Falls back
        # to plain send_message if the placeholder POST fails so we never
        # leave the user without a reply.
        placeholder_id = await self._send_thinking_placeholder(message.chat_id)

        from tesseract.mirror.server.ws import _start_channel_turn

        # CR-4 — progress narrative. While the turn runs, throttled edits
        # to the placeholder surface tool-call lifecycle + elapsed-time
        # pulses ("🔍 web_search: …", "🛠 still working — 30s in"). The
        # throttler self-caps at 1 Hz so a tool-heavy turn cannot trip
        # Telegram's edit rate limit. When ``placeholder_id`` is None
        # (placeholder send failed), we still pass a no-op edit_fn so
        # the turn logic doesn't branch on its presence.
        on_progress = self._build_progress_callback(message.chat_id, placeholder_id)
        throttler = on_progress._throttler  # for stop() on completion
        # `placeholder_id` is no longer fixed for the turn: every `<intent>`
        # retires one and opens the next. Read the live value back before
        # anything edits "the placeholder".
        progress_state = on_progress._state

        # Session 1 (2026-05-16) — recall context is *per-turn ephemeral*.
        # It wraps the body the model sees but is NOT persisted into the
        # conversation store (clean operator transcript) nor into the
        # chat session's history (would compound across turns). The
        # rolling summary + chat-tagged prior memories surface here so
        # The assistant picks up where yesterday left off automatically.
        # ``getattr`` guards fixture-only tests that bypass ``__init__``.
        turn_body = model_body
        chat_memory = getattr(self, "_chat_memory", None)

        # Session 3 (2026-05-16) — URL auto-extract. When the operator
        # shares a link, fetch its content via Tavily so the assistant sees the
        # actual page rather than just the URL string. Best-effort:
        # missing TAVILY_API_KEY / network failure degrades to no
        # extraction; the URLs still appear in the user body for the assistant
        # to comment on as text. Runs concurrently with the recall
        # query so the slower of the two bounds the wait.
        urls = find_urls(message.text)
        # The extraction is gated as `tavily_extract`, using this chat's own
        # context and ASK channel — the same decision the tool would get if
        # the assistant had asked for the page itself.
        chat_session = getattr(session, "chat_session", None)
        url_task = (
            asyncio.create_task(
                extract_urls_to_context(
                    urls,
                    context=getattr(chat_session, "tool_context", None),
                    ask_fn=getattr(chat_session, "ask_fn", None),
                    policy=getattr(chat_session, "policy", None),
                )
            )
            if urls
            else None
        )

        # When chat_memory is missing (minimal harness), still resolve the
        # URL task so it does not dangle. The result is dropped because
        # there's no recall_ctx to fold it into.
        if chat_memory is None and url_task is not None:
            await _cancel_task(url_task)

        if chat_memory is not None:
            try:
                recall_ctx = await chat_memory.recall_for_inbound(
                    self.name, chat_key, message.text or "",
                )
            except Exception:
                log.exception(
                    "telegram: recall_for_inbound failed for chat=%s", chat_key,
                )
                recall_ctx = ""

            # Fold URL content into the recall context block — same
            # wrapping so the assistant sees one unified "what to consider"
            # preamble per turn.
            if url_task is not None:
                try:
                    url_ctx = await url_task
                except Exception:
                    log.exception(
                        "telegram: url-extract task failed for chat=%s", chat_key,
                    )
                    url_ctx = ""
                if url_ctx:
                    recall_ctx = (
                        f"{recall_ctx}\n\n{url_ctx}" if recall_ctx else url_ctx
                    )

            if recall_ctx:
                # Neutralize an accidental ``</recall_context>`` in the
                # user's body so a pasted XML fragment can't close the
                # wrapper early and push the actual recall context outside
                # the tagged block. Replacement preserves the literal
                # intent (operator can still see "</recall_context>" they
                # typed) without breaking the wrapping structure.
                safe_body = model_body.replace(
                    "</recall_context>", "&lt;/recall_context&gt;",
                )
                turn_body = (
                    f"<recall_context>\n{recall_ctx}\n</recall_context>\n\n{safe_body}"
                )

        try:
            reply = await _start_channel_turn(
                self._app,
                session,
                channel=self.name,
                chat_id=chat_key,
                body=turn_body,
                on_progress=on_progress,
            )
        except Exception:
            log.exception("telegram: chat turn failed for chat_id=%s", message.chat_id)
            await throttler.stop()
            await _cancel_task(typing_task)
            placeholder_id = progress_state["placeholder_id"]
            if placeholder_id is not None:
                await self._edit_or_fallback(
                    message.chat_id,
                    placeholder_id,
                    "⚠️ I hit an error processing that. Try again?",
                )
            return
        await throttler.stop()
        placeholder_id = progress_state["placeholder_id"]
        await _cancel_task(typing_task)

        await compact_after_turn(
            session.chat_session,
            chat_memory=chat_memory, channel=self.name, chat_id=chat_key,
        )

        # A1 — workspace-gate visibility. Any forced-ASK posture (the 5
        # forced-ASK bash_security checks, file_write, agent_promote,
        # etc.) emits a ``agent_post`` workspace event via the channel
        # gate and returns False on the same turn. Without surfacing
        # that to the remote user, replies look like opaque refusals
        # ("I can't do that"). The per-turn emitted set holds one hash
        # per gated call; we quote its size so the operator on the
        # phone knows to switch to Mirror.
        per_turn = getattr(session, _PER_TURN_ATTR, None) or set()
        gated_n = len(per_turn) if isinstance(per_turn, set) else 0

        if reply:
            if gated_n:
                reply = (
                    reply.rstrip()
                    + "\n\n"
                    + _format_gated_footer(gated_n)
                )
            # Only attach a quote-reply when the placeholder send failed
            # — the placeholder edit already anchors the reply visually,
            # so doubling up with reply_to would look noisy. On the rare
            # placeholder-failed path the operator deserves the explicit
            # ``↩ <user msg>`` cue (Session 2 2026-05-16).
            reply_to = message.message_id if placeholder_id is None else None
            # Session 3 (2026-05-16) — per-chat voice-reply toggle. When
            # the operator flipped ``/voice_on``, synthesise the reply
            # via the configured TTS lane and ship as a voice note. Track
            # synthesis success separately from the cosmetic placeholder
            # edit so a failed placeholder edit (rate limit, message too
            # old) AFTER a successful voice send doesn't trigger a
            # duplicate text reply. Falls back to text only when the
            # voice synth itself failed.
            if self._state.poll_state.reply_voice.get(chat_key):
                voice_sent = False
                try:
                    await self.send_voice(
                        chat_ref=chat_key, text=reply,
                        reply_to_message_id=reply_to,
                    )
                    voice_sent = True
                except Exception:
                    log.exception(
                        "telegram: voice-reply synth failed for chat=%s; "
                        "falling back to text", chat_key,
                    )
                if voice_sent:
                    if placeholder_id is not None:
                        # Best-effort cosmetic — failure here is swallowed
                        # inside ``_edit_or_fallback``; the voice note is
                        # already delivered.
                        await self._edit_or_fallback(
                            message.chat_id, placeholder_id, "🎙",
                        )
                else:
                    await self._send_outbound(
                        message.chat_id, reply,
                        placeholder_id=placeholder_id,
                        reply_to_message_id=reply_to,
                    )
            else:
                await self._send_outbound(
                    message.chat_id, reply,
                    placeholder_id=placeholder_id,
                    reply_to_message_id=reply_to,
                )
            await offer_a_fresh_session(
                crossed_into_a_new_day=crossed_into_a_new_day,
                chat_session=session.chat_session,
                send=lambda text: self._send_outbound(message.chat_id, text),
            )
        elif placeholder_id is not None:
            # Turn produced no reply (cancellation, empty stream, or
            # every step gated). Replace the "thinking…" placeholder
            # with a specific message so the operator knows what to do.
            if gated_n:
                msg = _format_gated_footer(gated_n)
            else:
                msg = "(no reply produced this turn)"
            await self._edit_or_fallback(
                message.chat_id,
                placeholder_id,
                msg,
            )

    # -- tier + TTL helpers (audit fix M3) --------------------------------

    def _ttl_expired(self, chat_id: int) -> bool:
        """Return True when ``user_ttl[chat_id]`` is in the past.

        Malformed TTL strings are treated as expired so a corrupted state
        file errs on the side of *more* friction, not less. A chat with no
        TTL set (the operator-tier default) is never expired.
        """
        key = str(chat_id)
        ttl_iso = self._state.poll_state.user_ttl.get(key)
        if not ttl_iso:
            return False
        try:
            when = datetime.fromisoformat(ttl_iso)
        except (TypeError, ValueError):
            log.warning(
                "telegram: malformed user_ttl for chat=%s (%r); treating as expired",
                key, ttl_iso,
            )
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= when

    async def _auto_revoke_for_ttl(self, chat_id: int) -> None:
        """Move ``chat_id`` from allowlist back to pending; notify once."""
        key = str(chat_id)
        with self._state.with_lock():
            self._state.allowlist.chat_ids.discard(chat_id)
            self._state.allowlist.pending.setdefault(
                chat_id,
                PendingChat(
                    chat_id=chat_id,
                    username=self._state.poll_state.user_display.get(key),
                    first_seen=datetime.now(timezone.utc).isoformat(),
                ),
            )
            save_allowlist(self._state.allowlist_path, self._state.allowlist)
            self._state.poll_state.user_ttl.pop(key, None)
            save_state(self._state.state_path, self._state.poll_state)
        session = self._sessions.pop(chat_id, None)
        if session is not None:
            await self._cancel_session_turn(session)
        await self._safe_send(
            chat_id=chat_id,
            text=(
                "This chat's access has expired. Ask the operator to re-approve."
            ),
        )
        log.info("telegram: auto-revoked chat=%s on TTL expiry", chat_id)

    # -- offline-inbox drain (audit fix M1) -------------------------------

    async def drain_offline_inbox(self, *, chat_id: int | None = None) -> int:
        """Replay queued offline messages as real turns. Returns count replayed.

        ``chat_id`` scopes the drain to a single chat; ``None`` drains all.
        Each replay synthesises a :class:`TelegramMessage` from the
        stored ``OfflineMessage`` and routes it back through
        :meth:`_handle_message` with the override already flipped to
        online — the message reaches the chat turn the way it would
        have if the operator had been online when it arrived.
        Best-effort: a per-message failure is logged and the next
        message is still attempted so one poison message can't strand
        the rest of the queue.
        """
        from tesseract.integrations.telegram.api import TelegramMessage

        targets: list[str]
        with self._state.with_lock():
            if chat_id is None:
                targets = list(self._state.poll_state.offline_inbox.keys())
            else:
                targets = [str(chat_id)]
        replayed = 0
        for chat_key in targets:
            with self._state.with_lock():
                queue = self._state.poll_state.drain_offline(chat_key)
                save_state(self._state.state_path, self._state.poll_state)
            if not queue:
                continue
            try:
                cid = int(chat_key)
            except ValueError:
                log.warning("telegram: drain skipped malformed chat_key=%r", chat_key)
                continue
            await self._safe_send(
                chat_id=cid,
                text=(
                    f"the assistant is back online. Replaying {len(queue)} message"
                    f"{'s' if len(queue) != 1 else ''}…"
                ),
            )
            for msg in queue:
                synth = TelegramMessage(
                    update_id=0,
                    message_id=msg.telegram_message_id,
                    chat_id=cid,
                    chat_type="private",
                    from_user_id=msg.from_user_id,
                    from_username=msg.from_username,
                    text=msg.text,
                    date=0,
                )
                try:
                    await self._handle_message(synth)
                    replayed += 1
                except Exception:
                    log.exception(
                        "telegram: replay failed for chat=%s msg=%s",
                        chat_key, msg.telegram_message_id,
                    )
        if replayed:
            log.info("telegram: drained %d offline message(s)", replayed)
        return replayed

    def list_missed(self, chat_id: int) -> list[dict[str, object]]:
        """Read-only view of the offline-inbox tail for the Channels UI."""
        key = str(chat_id)
        rows = self._state.poll_state.offline_inbox.get(key, [])
        return [m.to_dict() for m in rows]

    # -- attachment decoding (CR-2) ---------------------------------------

    async def _decode_attachments(
        self,
        attachments: tuple[ChannelAttachment, ...],
        message: TelegramMessage | None = None,
    ) -> tuple[ChannelAttachment, ...]:
        if not attachments:
            return attachments
        decoded: list[ChannelAttachment] = []
        for att in attachments:
            if att.status != "no_handler":
                decoded.append(att)
                continue
            try:
                decoded.append(await self._decode_attachment(att, message))
            except Exception:
                # Decoder bug — preserve visibility by surfacing an
                # ``extract_failed`` so the assistant still sees something instead
                # of silently dropping the kind. Stack trace goes to the
                # backend log; the ``<error>`` body stays short.
                log.exception(
                    "telegram: decoder crashed for kind=%s (file_id=%s)",
                    att.kind, att.ref,
                )
                decoded.append(
                    dataclasses.replace(
                        att,
                        status="extract_failed",
                        error="internal decoder error",
                    )
                )
        return tuple(decoded)

    async def _decode_attachment(
        self, att: ChannelAttachment, message: TelegramMessage | None = None,
    ) -> ChannelAttachment:
        decoders = {
            "voice": self._decode_voice,
            "photo": self._decode_photo,
            "document": self._decode_document,
        }
        decoder = decoders.get(att.kind)
        if decoder is not None:
            return await decoder(att, message)
        # Session 1 (2026-05-16) — kinds without a text extractor still get
        # persisted when they carry a file_id, so operators can re-open
        # the video / audio / sticker / animation later and the assistant sees a
        # ``storage_path`` it can reference in a future turn (e.g. "edit
        # the GIF you sent yesterday"). Status stays ``no_handler`` — we
        # deliberately do not promote to ``ready`` because no text was
        # extracted.
        if att.kind in PERSISTED_KINDS:
            return await self._persist_undecoded(att, message)
        return att

    async def _persist_bytes(
        self,
        att: ChannelAttachment,
        message: TelegramMessage | None,
        data: bytes,
        *,
        default_mime: str | None = None,
    ) -> StoredChannelAttachment | None:
        """Save fetched bytes under ``uploads/channels``; log+swallow failure.

        Returns the :class:`StoredChannelAttachment` on success so the
        caller can stamp ``storage_path`` onto the decoded attachment.
        Persistence failure is non-fatal — the operator still gets the
        transcript / description, just no archived file.
        """
        if message is None:
            return None
        try:
            return await save_channel_attachment(
                channel=self.name,
                chat_id=str(message.chat_id),
                message_id=str(message.message_id),
                kind=att.kind,
                data=data,
                filename=att.filename,
                mime_type=(att.mime or default_mime or ""),
                source_ref=att.ref or "",
                caption=att.caption or "",
            )
        except Exception:
            log.exception(
                "telegram: failed to persist %s bytes for chat=%s msg=%s",
                att.kind, message.chat_id, message.message_id,
            )
            return None

    async def _persist_undecoded(
        self, att: ChannelAttachment, message: TelegramMessage | None,
    ) -> ChannelAttachment:
        """Fetch+store kinds we don't decode (video/audio/animation/sticker)."""
        if message is None:
            return att
        max_bytes: int | None = None
        cfg = self._channels_config()
        if cfg is not None and cfg.telegram.attachments is not None:
            # AttachmentCaps only declares voice/audio/photo/document/video;
            # sticker/animation/video_note are unschemaed and fall through
            # to the Bot API 20 MiB ceiling enforced by the fetcher.
            attachments_cfg = cfg.telegram.attachments
            if hasattr(attachments_cfg, att.kind):
                kind_cap = getattr(attachments_cfg, att.kind)
                max_bytes = getattr(kind_cap, "max_bytes", None)

        if max_bytes and att.size and att.size > max_bytes:
            return dataclasses.replace(
                att,
                status="too_large",
                error=f"{att.kind} {att.size}B exceeds {max_bytes}B cap",
            )

        fetched = await fetch_telegram_attachment(
            att.ref, api=self._api, max_bytes=max_bytes,
        )
        if isinstance(fetched, FetchRejection):
            return dataclasses.replace(
                att,
                status=_fetch_failure_to_status(fetched.kind),
                error=fetched.detail,
            )

        stored = await self._persist_bytes(att, message, fetched.data)
        if stored is None:
            return att
        return dataclasses.replace(att, storage_path=stored.storage_path)

    def _channels_config(self) -> ChannelsConfig | None:
        cfg = self._app.get("channels_config")
        return cfg if isinstance(cfg, ChannelsConfig) else None

    async def _decode_voice(
        self, att: ChannelAttachment, message: TelegramMessage | None = None,
    ) -> ChannelAttachment:
        engine = self._app.get("stt_engine")
        local_cfg = getattr(engine, "local_config", None) if engine is not None else None
        if local_cfg is None:
            return dataclasses.replace(
                att,
                status="extract_failed",
                error="local Whisper STT not configured",
            )

        max_seconds: int | None = None
        cfg = self._channels_config()
        if cfg is not None and cfg.telegram.attachments.voice is not None:
            max_seconds = cfg.telegram.attachments.voice.max_seconds
        if max_seconds and att.duration_s and att.duration_s > max_seconds:
            return dataclasses.replace(
                att,
                status="too_large",
                error=f"voice {att.duration_s}s exceeds {max_seconds}s cap",
            )

        fetched = await fetch_telegram_attachment(
            att.ref,
            api=self._api,
            max_bytes=None,
        )
        if isinstance(fetched, FetchRejection):
            return dataclasses.replace(
                att,
                status=_fetch_failure_to_status(fetched.kind),
                error=fetched.detail,
            )

        stored = await self._persist_bytes(
            att, message, fetched.data, default_mime="audio/ogg",
        )
        storage_path = stored.storage_path if stored is not None else None

        try:
            transcript = await transcribe_voice_audio(
                fetched.data,
                cfg=local_cfg,
                mime=att.mime,
            )
        except VoiceHandlerError as exc:
            return dataclasses.replace(
                att, status="extract_failed", error=str(exc),
                storage_path=storage_path,
            )

        text = (transcript or "").strip()
        if not text:
            return dataclasses.replace(
                att,
                status="extract_failed",
                error="transcript empty",
                storage_path=storage_path,
            )
        return dataclasses.replace(
            att, status="ready", extracted=text, storage_path=storage_path,
        )

    async def _decode_photo(
        self, att: ChannelAttachment, message: TelegramMessage | None = None,
    ) -> ChannelAttachment:
        max_bytes: int | None = None
        max_chars = 800
        cfg = self._channels_config()
        if cfg is not None:
            if cfg.telegram.attachments.photo is not None:
                max_bytes = cfg.telegram.attachments.photo.max_bytes
            if cfg.telegram.extract is not None:
                max_chars = cfg.telegram.extract.image_caption_chars
        if max_bytes and att.size and att.size > max_bytes:
            return dataclasses.replace(
                att,
                status="too_large",
                error=f"photo {att.size}B exceeds {max_bytes}B cap",
            )

        fetched = await fetch_telegram_attachment(
            att.ref,
            api=self._api,
            max_bytes=max_bytes,
        )
        if isinstance(fetched, FetchRejection):
            return dataclasses.replace(
                att,
                status=_fetch_failure_to_status(fetched.kind),
                error=fetched.detail,
            )

        mime = att.mime or "image/jpeg"
        stored = await self._persist_bytes(att, message, fetched.data, default_mime=mime)
        storage_path = stored.storage_path if stored is not None else None

        try:
            description = await describe_image(
                fetched.data,
                mime=mime,
                caption=att.caption,
                cost_ledger=self._app.get("cost_ledger"),
                max_chars=max_chars,
            )
        except ImageHandlerError as exc:
            return dataclasses.replace(
                att, status="extract_failed", error=str(exc),
                storage_path=storage_path,
            )

        return dataclasses.replace(
            att, status="ready", extracted=description, storage_path=storage_path,
        )

    async def _decode_document(
        self, att: ChannelAttachment, message: TelegramMessage | None = None,
    ) -> ChannelAttachment:
        max_bytes: int | None = None
        max_chars = 6000
        image_max_chars = 800
        cfg = self._channels_config()
        if cfg is not None:
            if cfg.telegram.attachments.document is not None:
                max_bytes = cfg.telegram.attachments.document.max_bytes
            if cfg.telegram.extract is not None:
                max_chars = cfg.telegram.extract.document_chars
                image_max_chars = cfg.telegram.extract.image_caption_chars
        if max_bytes and att.size and att.size > max_bytes:
            return dataclasses.replace(
                att,
                status="too_large",
                error=f"document {att.size}B exceeds {max_bytes}B cap",
            )

        fetched = await fetch_telegram_attachment(
            att.ref,
            api=self._api,
            max_bytes=max_bytes,
        )
        if isinstance(fetched, FetchRejection):
            return dataclasses.replace(
                att,
                status=_fetch_failure_to_status(fetched.kind),
                error=fetched.detail,
            )

        stored = await self._persist_bytes(att, message, fetched.data)
        storage_path = stored.storage_path if stored is not None else None

        # Telegram lets users send images as "documents" (drag-drop file
        # rather than photo-share). The Telegram client typed it
        # kind="document" but the mime is image/*. Route to the vision
        # extractor so the operator gets a description, not a
        # "no extractor for document mime/suffix=image/jpeg" rejection.
        normalized_mime = (att.mime or "").split(";", 1)[0].strip().lower()
        if normalized_mime.startswith("image/"):
            try:
                description = await describe_image(
                    fetched.data,
                    mime=normalized_mime,
                    caption=att.caption,
                    cost_ledger=self._app.get("cost_ledger"),
                    max_chars=image_max_chars,
                )
            except ImageHandlerError as exc:
                return dataclasses.replace(
                    att, status="extract_failed", error=str(exc),
                    storage_path=storage_path,
                )
            return dataclasses.replace(
                att, status="ready", extracted=description, storage_path=storage_path,
            )

        try:
            text = await extract_document_text(
                fetched.data,
                mime=att.mime,
                filename=att.filename,
                max_chars=max_chars,
            )
        except DocumentHandlerError as exc:
            return dataclasses.replace(
                att, status="extract_failed", error=str(exc),
                storage_path=storage_path,
            )

        text = (text or "").strip()
        if not text:
            return dataclasses.replace(
                att,
                status="extract_failed",
                error="document empty after extract",
                storage_path=storage_path,
            )
        return dataclasses.replace(
            att, status="ready", extracted=text, storage_path=storage_path,
        )

    # -- outbound replies -------------------------------------------------

    async def _send_outbound(
        self,
        chat_id: int,
        body: str,
        *,
        placeholder_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Chunk-aware outbound send (audit fix M2).

        Long bodies are split on paragraph boundaries via
        :func:`chunk_for_telegram`. The first chunk lands in the
        ``thinking…`` placeholder (edit), subsequent chunks are fresh
        sends. The *full* unchunked body is persisted to the conversation
        store so downstream readers (Mirror UI history pane, gate
        transcript-tail) never see truncated text.

        ``reply_to_message_id`` (Session 2 2026-05-16) — when set, the
        first FRESH chunk (i.e. not an edit) attaches a Telegram
        quote-reply to that message. Edits never carry reply_to (already
        anchored to the placeholder). Subsequent chunks omit it so the
        thread doesn't repeat the quote.
        """
        assert self._api is not None
        chunks = chunk_for_telegram(body)
        if not chunks:
            return
        reply_to_consumed = False
        for idx, chunk in enumerate(chunks):
            html = markdown_to_telegram_html(chunk)
            if idx == 0 and placeholder_id is not None:
                sent = await self._edit_or_resend(
                    chat_id=chat_id,
                    message_id=placeholder_id,
                    html=html,
                    plain=chunk,
                )
                if not sent:
                    return
            else:
                this_reply_to = (
                    reply_to_message_id if not reply_to_consumed else None
                )
                sent = await self._send_fresh(
                    chat_id=chat_id,
                    html=html,
                    plain=chunk,
                    reply_to_message_id=this_reply_to,
                )
                if not sent:
                    return
                if this_reply_to is not None:
                    reply_to_consumed = True
        chat_key = str(chat_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        # Persist the *full* body, not a chunk. Downstream readers
        # (history pane, gate transcript) must never see truncated text.
        self._conversations.append(
            self.name,
            chat_key,
            ChannelMessage(
                ts=now_iso,
                direction="outbound",
                body=body,
                extra={"chunks": len(chunks)} if len(chunks) > 1 else {},
            ),
        )
        with self._state.with_lock():
            self._state.poll_state.messages_out_total[chat_key] = (
                self._state.poll_state.messages_out_total.get(chat_key, 0) + 1
            )
            self._state.poll_state.record_outbound(chat_key, now_iso)
            save_state(self._state.state_path, self._state.poll_state)

    async def _edit_or_resend(
        self, *, chat_id: int, message_id: int, html: str, plain: str
    ) -> bool:
        """Edit ``message_id`` to ``html``; fall back to plain edit, then fresh send."""
        assert self._api is not None
        try:
            await self._api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=html,
                parse_mode="HTML",
            )
            return True
        except TelegramAPIError as exc:
            log.warning(
                "telegram: editMessageText (HTML) failed for chat=%s (%s); falling back",
                chat_id, exc,
            )
        try:
            await self._api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=plain,
            )
            return True
        except TelegramAPIError as exc:
            log.warning(
                "telegram: editMessageText (plain) failed for chat=%s (%s); fresh-send",
                chat_id, exc,
            )
        return await self._send_fresh(chat_id=chat_id, html=html, plain=plain)

    async def _send_fresh(
        self, *, chat_id: int, html: str, plain: str,
        reply_to_message_id: int | None = None,
    ) -> bool:
        assert self._api is not None
        try:
            await self._api.send_message(
                chat_id=chat_id,
                text=html,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
            )
            return True
        except TelegramAPIError as exc:
            log.warning(
                "telegram: sendMessage (HTML) failed for chat=%s (%s); retrying plain",
                chat_id, exc,
            )
        try:
            await self._api.send_message(
                chat_id=chat_id, text=plain,
                reply_to_message_id=reply_to_message_id,
            )
            return True
        except TelegramAPIError as exc:
            log.warning(
                "telegram: sendMessage (plain) also failed for chat=%s (%s)",
                chat_id, exc,
            )
            return False

    def _build_progress_callback(self, chat_id: int, placeholder_id: int | None):
        """Build an ``on_progress`` lambda + its 1 Hz throttler for one turn.

        Returns a callable whose ``._throttler`` attribute holds the
        :class:`ProgressThrottler` so the caller can ``stop()`` it on
        turn completion. The lambda formats each :class:`ProgressEvent`
        via :func:`format_progress_line` and pumps it through the
        throttler, which calls back into ``_edit_progress`` (idempotent
        editMessageText, plain-text-only). HTML for the final reply is
        applied by ``_send_outbound`` after the turn lands; progress
        lines stay plain so we never trip Telegram's HTML parser
        mid-stream.

        If ``placeholder_id`` is ``None`` (placeholder send failed),
        the edit_fn is a no-op so the turn still runs and lands its
        reply via the fresh-send fallback path.
        """
        # Mutable because an `<intent>` retires the placeholder it lands in
        # and opens a fresh one. The caller reads it back off the callback so
        # the final reply edits whichever placeholder is current.
        state: dict[str, int | None] = {"placeholder_id": placeholder_id}

        async def edit_fn(text: str) -> None:
            pid = state["placeholder_id"]
            if pid is None:
                return
            await self._edit_progress(chat_id, pid, text)

        throttler = ProgressThrottler(edit_fn)

        async def _on_progress(event: ProgressEvent) -> None:
            line = format_progress_line(event)
            if not line:
                return
            if event.kind == "intent":
                # The assistant said this, so it stays in the chat: the
                # placeholder holding it is retired and the rest of the turn
                # continues under a new one. Same funnel as the cockpit, where
                # the intent is a block in the transcript rather than a status
                # line that the answer overwrites.
                #
                # The swap happens FIRST so a coalesced flush still in flight
                # lands on the new placeholder rather than overwriting the
                # sentence in the old one.
                retiring = state["placeholder_id"]
                state["placeholder_id"] = await self._send_thinking_placeholder(chat_id)
                if retiring is not None:
                    await self._edit_progress(chat_id, retiring, line)
                return
            await throttler.emit(line)

        _on_progress._throttler = throttler  # type: ignore[attr-defined]
        _on_progress._state = state  # type: ignore[attr-defined]
        return _on_progress

    async def _edit_progress(self, chat_id: int, message_id: int, text: str) -> None:
        """Plain-text editMessageText for in-turn progress lines.

        Errors are swallowed — a flaky edit must not abort the turn or
        the final reply. ``send_outbound`` will overwrite the
        placeholder with the final HTML body on success.
        """
        if self._api is None:
            return
        try:
            await self._api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text[:_TELEGRAM_TEXT_MAX],
            )
        except TelegramAPIError as exc:
            # Most common reason: "message is not modified" when the
            # rendered line is identical to the last edit. Log at debug
            # so legitimate flakes still get visibility via the
            # surrounding turn-failure logs.
            log.debug(
                "telegram: progress edit failed for chat=%s (%s)",
                chat_id, exc,
            )

    async def _safe_react(
        self, chat_id: int, message_id: int, emoji: str | None,
    ) -> None:
        """Best-effort ``setMessageReaction`` — log + swallow on failure.

        ``emoji=None`` clears any prior reaction. Used both for the
        instant-ack pulse on inbound (💭) and by the public
        :meth:`react_to_message` surface which the assistant calls via the
        ``channel_react`` kernel tool.
        """
        if self._api is None:
            return
        try:
            await self._api.set_message_reaction(
                chat_id=chat_id, message_id=message_id, emoji=emoji,
            )
        except TelegramAPIError as exc:
            log.debug(
                "telegram: setMessageReaction failed for chat=%s msg=%s emoji=%r (%s)",
                chat_id, message_id, emoji, exc,
            )

    async def react_to_message(
        self,
        *,
        chat_ref: str,
        message_id: int,
        emoji: str | None,
    ) -> None:
        """Public surface for the ``channel_react`` kernel tool.

        ``emoji=None`` clears a prior reaction. Errors propagate so the
        tool layer can surface them as ``ToolResult(is_error=True)``;
        the inbound auto-ack path uses :meth:`_safe_react` instead.
        """
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        chat_id = self._coerce_chat_ref(chat_ref)
        await self._api.set_message_reaction(
            chat_id=chat_id, message_id=int(message_id), emoji=emoji,
        )

    async def _typing_keepalive(self, chat_id: int) -> None:
        """Re-fire ``sendChatAction("typing")`` every 4 s until cancelled.

        Telegram dismisses the typing indicator after ~5 s of silence so
        a one-shot firing at turn-start makes long turns look dead. The
        4-s cadence keeps the dot continuously visible without flooding
        the chat-action endpoint (Telegram tolerates much higher rates
        but 4 s matches the documented dismissal window with a 1 s
        safety margin). ``TelegramAPIError`` failures are swallowed —
        a dropped chat-action does not warrant aborting the turn.
        """
        if self._api is None:
            return
        try:
            while True:
                try:
                    await self._api.send_chat_action(chat_id=chat_id, action="typing")
                except TelegramAPIError:
                    pass
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            return

    async def _send_thinking_placeholder(self, chat_id: int) -> int | None:
        assert self._api is not None
        try:
            result = await self._api.send_message(chat_id=chat_id, text="◉ thinking…")
        except TelegramAPIError as exc:
            log.warning("telegram: thinking-placeholder send failed for chat=%s (%s)", chat_id, exc)
            return None
        msg_id = result.get("message_id") if isinstance(result, dict) else None
        return int(msg_id) if isinstance(msg_id, int) else None

    async def _edit_or_fallback(self, chat_id: int, message_id: int, text: str) -> None:
        assert self._api is not None
        try:
            await self._api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except TelegramAPIError as exc:
            log.warning(
                "telegram: editMessageText fallback failed for chat=%s (%s); sending fresh",
                chat_id, exc,
            )
            try:
                await self._api.send_message(chat_id=chat_id, text=text)
            except TelegramAPIError as exc2:
                log.warning(
                    "telegram: fresh-send fallback also failed for chat=%s (%s)",
                    chat_id, exc2,
                )

    # -- chat-session ownership -------------------------------------------

    def _session_for(self, chat_id: int, *, reset: bool) -> ServerSession:
        existing = self._sessions.get(chat_id)
        if existing is not None and not reset:
            return existing
        if existing is not None:
            # Cancel any in-flight turn on the old session before dropping
            # the reference so the next message does not race against a
            # zombie task that thinks it still owns the chat history.
            asyncio.create_task(
                self._cancel_session_turn(existing),
                name=f"telegram:reset:{chat_id}",
            )
        session = self._build_headless_session(chat_id)
        self._sessions[chat_id] = session
        return session

    def _build_headless_session(self, chat_id: int) -> ServerSession:
        session_id = f"telegram_{chat_id}_{uuid.uuid4().hex[:8]}"
        cfg = self._channels_config()
        display_name = cfg.telegram.display_name if cfg is not None else "Telegram"
        chat_session = _build_chat_session(
            self._app,
            session_id,
            _deny_ask,
            _noop_cli_sink,
            _deny_overage,
            _noop_status_emit,
            kind="channel",
            channel_display_name=display_name,
        )
        session = ServerSession(
            session_id=session_id,
            ws=_NullWebSocket(),
            chat_session=chat_session,
            event_log=EventLog(),
            pending_asks={},
            pending_overage_asks={},
            kind="channel",
        )

        chat_key = str(chat_id)
        # `channel_tier` was stashed here for the friend-tier hooks; both the
        # denylist and the tier-based error redaction are gone (2026-08-15)
        # and nothing reads it, so it is not set. Who may talk to this bot at
        # all remains the allowlist's job — `is_allowed` / `is_blocked` — and
        # that is untouched.
        setattr(session, "channel_chat_id", chat_key)

        # P6 §G3 (idle-wake-design.md) — wire spawn-wake parity onto headless
        # sessions. Bridge sessions have exactly one chat
        # (``session.active_chat_id`` is the internal registry key), so the
        # notifier/idle/dedup logic is identical to the cockpit path; only
        # the wake DELIVERY leg differs (channel turn + real send path,
        # below in ``_wake_turn_driver``).
        spawn_wake.wire_chat(
            self._app, session, session.active_chat_id, chat_session,
            turn_driver=self._wake_turn_driver,
        )

        # CR-5 — install the channel-aware ASK gate. ``gate_policy.on_ask``
        # picks between ``workspace_nudge`` (ask the operator on the
        # workspace inbox + the Telegram keyboard and wait for the answer,
        # exactly as the cockpit's ``ask_fn`` waits on its WS reply) and
        # ``deny`` (legacy ``_deny_ask`` semantics for any channel that
        # explicitly opts back in).
        on_ask = "workspace_nudge"
        decision_timeout_s = GatePolicy().decision_timeout_s
        if cfg is not None and cfg.telegram.gate_policy is not None:
            on_ask = cfg.telegram.gate_policy.on_ask
            decision_timeout_s = cfg.telegram.gate_policy.decision_timeout_s

        if on_ask == "workspace_nudge":
            event_store = self._app.get("workspace_event_store")
            inner_ask = build_channel_ask_fn(
                session=session,
                channel=self.name,
                chat_id=chat_key,
                display_name=display_name,
                event_store=event_store,
                conversation_store=getattr(self, "_conversations", None),
                pending_asks=self._channel_asks(),
                decision_timeout_s=decision_timeout_s,
                ask_on_channel=self._build_channel_asker(chat_key),
            )
            # The friend-tier denylist that used to wrap this is gone
            # (2026-08-15). It was installed only around the ask_fn, so any
            # posture resolving straight to AUTO — every tool in the
            # `headless` override block — skipped it entirely: the gate never
            # reached the ASK branch, and the wrapper never ran. It denied
            # nothing in the one mode where it mattered while reading like a
            # restriction, which is worse than not being there. This install
            # is single-operator; who may talk to the bot at all is the
            # allowlist's job (`is_allowed` / `is_blocked`), and that stands.
            ask_fn = inner_ask
            # ``chat_session`` is a MagicMock in some fixture-only tests that
            # bypass ``_build_chat_session``; guard so the gate install does
            # not crash on missing attributes there.
            try:
                chat_session.ask_fn = ask_fn
                chat_session.tool_context.ask_fn = ask_fn
            except AttributeError:
                log.debug("channel gate: skipped ask_fn install on stub chat_session")

        return session

    def _channel_asks(self) -> PendingAsks:
        """The pending-ask registry, lazily allocated.

        ``__init__`` always creates it. The lazy path exists for tests that
        build a partial bridge with ``__new__`` to exercise session wiring
        without a network stack — the same seam ``_conversations`` is read
        through. Allocating here rather than raising keeps that working
        without letting two callers hold different dicts: the first call
        installs the one everything else then reads.
        """
        existing = getattr(self, "_pending_channel_asks", None)
        if existing is None:
            existing = {}
            self._pending_channel_asks = existing
        return existing

    def _build_workspace_broadcaster(self):
        """Return a coroutine that fans a workspace event to attached
        Mirror sessions so the operator sees the gate land without a
        manual refresh. Best-effort: import errors / send failures are
        logged and swallowed so a busted broadcaster cannot block the
        gate.

        2026-05-17: when the gated event originates from THIS bridge's
        channel, also push an inline-keyboard prompt to the operator's
        Telegram thread so the ASK is actionable from the phone. Without
        this, an ASK fired during a Telegram conversation only surfaces
        in the Mirror workspace inbox — invisible to the operator on
        their phone (the 17:13 svg_to_png incident).
        """
        app = self._app
        channel = self.name

        async def _broadcast(event) -> None:
            try:
                from tesseract.workspace_events.broadcast import (
                    broadcast_workspace_event,
                )
                await broadcast_workspace_event(app, event)
            except Exception:
                log.exception(
                    "telegram: workspace broadcast failed for gate event %s",
                    getattr(event, "event_id", "?"),
                )
        return _broadcast

    def _build_channel_asker(self, chat_id: int):
        """Return the gate's ``ask_on_channel`` for this chat.

        The question goes to the thread the request came from, and nowhere
        else. This is the whole wall: the operator answers on the surface they
        are already holding, exactly as the cockpit answers in the cockpit.
        """

        async def _ask(prompt_id: str, tool_name: str, args: dict, reason: str) -> bool:
            return await self._send_approval_prompt(
                chat_id=chat_id, prompt_id=prompt_id,
                tool_name=tool_name, reason=reason,
            )

        return _ask

    async def _send_approval_prompt(
        self, *, chat_id: int, prompt_id: str, tool_name: str, reason: str,
    ) -> bool:
        """Push an inline-keyboard ASK prompt to the chat, and say if it landed.

        Returns False when the operator cannot see the question, so the gate
        refuses immediately instead of parking the chat on a prompt that was
        never delivered. Stores ``(message_id, prompt_id, tool_name)`` on
        ``_pending_approval_messages`` so the callback handler can edit the
        prompt to "✓ Approved" / "✗ Rejected" once they tap.
        """
        if self._api is None:
            return False
        reason = (reason or "").strip()
        event_id = prompt_id
        # callback_data cap is 64 bytes. event_id is ~16 chars hex, so
        # `g:<event_id>:a` fits comfortably (~20 bytes).
        cb_approve = f"g:{event_id}:a"
        cb_reject = f"g:{event_id}:r"
        body = f"the assistant asks to call `{tool_name}`."
        if reason:
            body += f"\n_Reason:_ {reason[:200]}"
        body += "\n\nTap to decide:"
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✓ Approve", "callback_data": cb_approve},
                    {"text": "✗ Reject", "callback_data": cb_reject},
                ],
            ],
        }
        try:
            result = await self._api.send_message(
                chat_id=chat_id,
                text=body,
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        except TelegramAPIError as exc:
            log.warning("approval_prompt: send_message failed: %s", exc)
            return False
        msg_id = (
            (result.get("result") or {}).get("message_id")
            if isinstance(result.get("result"), dict)
            else result.get("message_id")
        )
        if msg_id is None:
            log.warning(
                "approval_prompt: Telegram accepted the send but returned no "
                "message_id — treating %s as undelivered", prompt_id,
            )
            return False
        self._pending_approval_messages[event_id] = {
            "chat_id": chat_id,
            "message_id": int(msg_id),
            "tool_name": tool_name,
        }
        return True

    async def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        """Dispatch an inline-keyboard tap from the operator's Telegram.

        Decodes ``callback_data`` (``g:<event_id>:{a,r}``), looks up the
        ServerSession for the chat, applies the same approval / rejection
        side-effects the Mirror UI runs, edits the original prompt to
        reflect the decision, and dismisses the spinner via
        ``answerCallbackQuery``.
        """
        if self._api is None:
            return
        cb_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        from_user = callback.get("from") or {}
        chat = (callback.get("message") or {}).get("chat") or {}
        try:
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            log.warning("telegram: callback dropped — no chat id in %r", chat)
            await self._safe_answer_callback(cb_id, "Bad chat context.")
            return
        message_id = (callback.get("message") or {}).get("message_id")
        # Operator-only — the gate's approval token mutates session state,
        # never let a friend-tier user flip it.
        tier = self._state.poll_state.user_tier.get(str(chat_id), "operator")
        if tier != "operator":
            log.warning(
                "telegram: callback refused — chat=%s tier=%s is not operator",
                chat_id, tier,
            )
            await self._safe_answer_callback(cb_id, "Not authorized.", show_alert=True)
            return
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "g":
            log.warning("telegram: callback data not a gate decision: %r", data)
            await self._safe_answer_callback(cb_id, "Unknown action.")
            return
        _, event_id, action = parts
        if action not in {"a", "r"}:
            log.warning("telegram: callback verb %r is neither approve nor reject", action)
            await self._safe_answer_callback(cb_id, "Unknown action.")
            return
        log.info(
            "telegram: callback %s for %s from chat=%s",
            "approve" if action == "a" else "reject", event_id, chat_id,
        )
        # Housekeeping only — the row exists so a stale prompt does not leak;
        # the decision itself is keyed on the parked future, not on it.
        self._pending_approval_messages.pop(event_id, None)
        approved = action == "a"
        # The turn is parked on this future. Resolving it IS the approval —
        # there is no token to record and nothing to retry later, because the
        # call the operator is looking at has not happened yet and is waiting
        # on this answer. `update_event_status` is the gate's job once it
        # wakes, so that one event is closed by one writer.
        entry = resolve_channel_ask(
            self._channel_asks(), event_id, approved=approved,
        )
        if entry is None:
            # Nobody is waiting: the wait timed out, a new message cancelled
            # it, or Mirror answered a moment earlier. Say so rather than
            # reporting a decision that changed nothing.
            log.info(
                "telegram: callback %s arrived after the call stopped waiting",
                event_id,
            )
            await self._safe_answer_callback(
                cb_id, "Too late — that call already stopped waiting.",
            )
            if message_id is not None:
                await self._safe_strip_keyboard(
                    chat_id, int(message_id),
                    suffix="\n\n⏱ Expired — the assistant stopped waiting for an answer.",
                )
            return
        if message_id is not None:
            await self._safe_strip_keyboard(
                chat_id, int(message_id),
                suffix=(
                    f"\n\n✓ Approved — running `{entry.tool_name}` now."
                    if approved else "\n\n✗ Rejected."
                ),
            )
        await self._safe_answer_callback(cb_id, "Approved." if approved else "Rejected.")
        log.info(
            "telegram: callback %s event=%s by user=%s",
            "approve" if action == "a" else "reject", event_id,
            (from_user.get("username") or from_user.get("id") or "?"),
        )

    async def _safe_answer_callback(
        self, cb_id: str, text: str, *, show_alert: bool = False,
    ) -> None:
        if not cb_id or self._api is None:
            return
        try:
            await self._api.answer_callback_query(
                callback_query_id=cb_id, text=text, show_alert=show_alert,
            )
        except TelegramAPIError as exc:
            log.warning("telegram: answer_callback_query failed: %s", exc)

    async def _safe_strip_keyboard(
        self, chat_id: int, message_id: int, *, suffix: str = "",
    ) -> None:
        """Remove the inline keyboard and optionally append a status line.

        Telegram's editMessageReplyMarkup with empty markup is the
        documented way to drop the keyboard without re-rendering text.
        When we want to also tag the message ("✓ Approved"), do a
        text edit instead so the disposition is visible in the chat log.
        """
        if self._api is None:
            return
        if not suffix:
            try:
                await self._api.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []},
                )
            except TelegramAPIError as exc:
                log.warning("telegram: edit_message_reply_markup failed: %s", exc)
            return
        try:
            await self._api.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"_(approval prompt closed){suffix}_",
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except TelegramAPIError as exc:
            log.warning("telegram: edit_message_text failed (callback): %s", exc)

    async def _cancel_session_turn(self, session: ServerSession) -> None:
        task = session.current_turn_task
        if task is None or task.done():
            return
        session.chat_session.tool_context.cancel_event.set()
        task.cancel()
        try:
            await task
        except Exception:
            pass

    # -- /clear follow-up (2026-05-16) ------------------------------------

    async def _handle_pending_clear_followup(
        self, message: TelegramMessage, chat_key: str, tier: str,
    ) -> bool:
        """Resolve a previously-issued /clear command.

        Returns True iff this message was a yes/no answer to /clear and
        was fully handled (clear performed, reply sent). Returns False
        when there's no pending /clear, when the pending stamp has
        expired, or when the body is anything other than yes/no — in
        which case the pending stamp is dropped silently and the
        caller falls through to normal processing.
        """
        del tier  # both tiers may answer their own /clear
        stamp = self._state.poll_state.pending_clear.get(chat_key)
        if not stamp:
            return False
        if _pending_clear_expired(stamp):
            with self._state.with_lock():
                self._state.poll_state.pending_clear.pop(chat_key, None)
                save_state(self._state.state_path, self._state.poll_state)
            return False

        body = (message.text or "").strip().lower()
        # Always clear the pending stamp before branching so a crash
        # in the reflection / clear path doesn't leave the chat locked.
        with self._state.with_lock():
            self._state.poll_state.pending_clear.pop(chat_key, None)
            save_state(self._state.state_path, self._state.poll_state)

        if body in _CLEAR_YES_TOKENS:
            try:
                await self._run_clear_reflection(message, chat_key)
            except Exception:
                log.exception(
                    "telegram: /clear reflection turn failed for chat=%s",
                    message.chat_id,
                )
            self.clear_session(message.chat_id)
            await self._safe_send(
                chat_id=message.chat_id,
                text="🧹 Reflected and cleared. Next message starts a fresh thread.",
            )
            return True
        if body in _CLEAR_NO_TOKENS:
            self.clear_session(message.chat_id)
            await self._safe_send(
                chat_id=message.chat_id,
                text="🧹 Cleared. Next message starts a fresh thread.",
            )
            return True
        # Anything else cancels — fall through, no reply yet so the
        # normal turn handles it.
        await self._safe_send(
            chat_id=message.chat_id,
            text="/clear cancelled — processing your message normally.",
        )
        return False

    async def _handle_agenda_quick_reply(
        self, message: TelegramMessage, text_stripped: str,
    ) -> bool:
        """AU-10 — route ``<agenda_id>:<verb>`` to the AgendaStore.

        Returns True iff the message matched the quick-reply pattern AND
        an operator-visible reply was sent. Any other text returns False
        so the bridge falls through to the command router / chat turn.
        """
        from tesseract.integrations.telegram.agenda_quick_reply import (
            apply_quick_reply,
            format_reply_body,
            looks_like_quick_reply,
            parse_quick_reply,
        )

        if not looks_like_quick_reply(text_stripped):
            return False
        reply = parse_quick_reply(text_stripped)
        if reply is None:
            return False
        store = self._app.get("agenda_store") if self._app is not None else None
        if store is None:
            try:
                from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

                store = AgendaStore()
            except Exception:
                log.exception("telegram: agenda quick-reply could not resolve store")
                return False
        try:
            result = await apply_quick_reply(reply, store=store)
        except Exception:
            log.exception(
                "telegram: agenda quick-reply apply crashed for %s", reply.agenda_id,
            )
            await self.send_text(
                chat_ref=str(message.chat_id),
                text=f"<b>Quick reply</b> · <code>{reply.agenda_id}</code> · backend error",
            )
            return True
        body = format_reply_body(result)
        await self.send_text(chat_ref=str(message.chat_id), text=body)
        return True

    def clear_session(self, chat_id: int) -> None:
        """Drop the in-memory chat session for ``chat_id``.

        The next inbound message rebuilds a fresh ``ServerSession``
        with empty history. The persisted transcript in
        ``conversation_store`` is untouched — operator can still scroll
        back from the Mirror Channels tab.
        """
        existing = self._sessions.pop(chat_id, None)
        if existing is not None:
            asyncio.create_task(
                self._cancel_session_turn(existing),
                name=f"telegram:clear:{chat_id}",
            )

    async def _run_clear_reflection(
        self, message: TelegramMessage, chat_key: str,
    ) -> None:
        """Run one synthetic turn asking the assistant to reflect before clear.

        Reuses ``_start_channel_turn`` so the reflection flows through
        the same channel-prompt / persistence machinery as a normal
        turn. The model's reply is sent to the operator's chat. The
        session is then dropped by the caller.
        """
        from tesseract.mirror.server.ws import _start_channel_turn

        session = self._session_for(message.chat_id, reset=False)
        reset_per_turn_state(session)
        synth_body = (
            "[clear-reflect] I'm closing this thread. Reflect briefly "
            "on what we discussed in one short paragraph. If anything "
            "is worth keeping, persist it via diary_append or "
            "memory_save. Keep it tight — the thread closes after "
            "your reply."
        )
        placeholder_id = await self._send_thinking_placeholder(message.chat_id)
        on_progress = self._build_progress_callback(message.chat_id, placeholder_id)
        throttler = on_progress._throttler  # noqa: SLF001
        try:
            reply = await _start_channel_turn(
                self._app,
                session,
                channel=self.name,
                chat_id=chat_key,
                body=synth_body,
                on_progress=on_progress,
            )
        finally:
            await throttler.stop()
        if reply:
            await self._send_outbound(message.chat_id, reply, placeholder_id=placeholder_id)
        elif placeholder_id is not None:
            await self._edit_or_fallback(
                message.chat_id, placeholder_id,
                "(reflection produced no text — clearing anyway)",
            )

    async def _wake_turn_driver(self, app: Any, session: ServerSession, chat_id: str) -> str | None:
        """Channel-shaped spawn-wake turn driver (idle-wake-design.md §G3).

        Parameterized counterpart to the cockpit path (``_run_chat_turn``,
        wired via ``spawn_wake._wake_turn``'s default) — reuses the
        ``_run_clear_reflection`` pattern so the wake turn flows through
        ``_start_channel_turn`` and its reply is delivered via the bridge's
        real send path (placeholder edit / fresh send) rather than being
        dropped, since ``_start_channel_turn`` only returns reply text and
        does not itself send to Telegram.

        Returns ``None`` on a clean turn or the turn-level error text on a
        failure observed via ``_start_channel_turn``'s ``error_out`` — the
        shared ``spawn-wake`` breaker (``spawn_wake._wake_turn``) records it
        as a failure even though no exception propagates here (fix pass 1,
        idle-wake-design.md §G1).
        """
        from tesseract.mirror.server.spawn_wake import wake_nudge_text
        from tesseract.mirror.server.ws import _start_channel_turn

        # Task 6.3 — the label is stashed keyed by this internal registry
        # chat_id (wire_chat's chat_id), so the lookup must happen before it
        # is discarded in favor of the Telegram-facing chat_key below.
        wake_body = wake_nudge_text(session, chat_id)
        del chat_id  # internal registry key; delivery targets the Telegram chat id below

        chat_key = getattr(session, "channel_chat_id", None) or session.active_chat_id
        tg_chat_id = int(chat_key)
        # CR-5 — clear the per-turn gate-dedup set before the turn runs, same
        # as the two existing channel-turn call sites (inbound message
        # handling, ``_run_clear_reflection``). Without this a tool+args hash
        # gated in a prior turn was silently suppressed during wake turns —
        # the operator never saw the ASK nudge (fix pass 1).
        reset_per_turn_state(session)
        placeholder_id = await self._send_thinking_placeholder(tg_chat_id)
        on_progress = self._build_progress_callback(tg_chat_id, placeholder_id)
        throttler = on_progress._throttler  # noqa: SLF001
        # ``schedule_wake`` pre-registers THIS driver's own wrapper task into
        # ``current_turn_tasks[active_chat_id]`` (needed so `chat_idle()`
        # reports busy while the wake runs). Bridge sessions have exactly
        # one chat, so that is the SAME slot `_start_channel_turn`'s
        # busy-check reads via `session.current_turn_task`. Left alone, the
        # busy-check would find the currently-running wrapper task (not
        # done — we're inside it) and await itself — a self-await asyncio
        # rejects. Clear it first: `_start_channel_turn` re-registers its
        # own inner task for the turn's duration and clears it on exit, so
        # `chat_idle()` sees the chat busy throughout regardless.
        session.current_turn_task = None
        error_out: list[str] = []
        try:
            reply = await _start_channel_turn(
                app,
                session,
                channel=self.name,
                chat_id=chat_key,
                body=wake_body,
                on_progress=on_progress,
                error_out=error_out,
            )
        finally:
            await throttler.stop()
        if reply:
            await self._send_outbound(tg_chat_id, reply, placeholder_id=placeholder_id)
        elif placeholder_id is not None:
            await self._edit_or_fallback(
                tg_chat_id, placeholder_id,
                "(background task finished — nothing to add)",
            )
        return error_out[0] if error_out else None

    # -- status / replies --------------------------------------------------

    async def _reply_status(self, chat_id: int, *, offline: bool) -> None:
        if offline:
            text = "the assistant status: offline"
        else:
            session = self._sessions.get(chat_id)
            busy = (
                session is not None
                and session.current_turn_task is not None
                and not session.current_turn_task.done()
            )
            text = "the assistant status: busy" if busy else "the assistant status: online"
        await self._safe_send(chat_id=chat_id, text=text)

    async def _safe_send(self, *, chat_id: int, text: str) -> None:
        if self._api is None:
            return
        try:
            await self._api.send_message(chat_id=chat_id, text=text[:_TELEGRAM_TEXT_MAX])
        except TelegramAPIError as exc:
            log.warning("telegram: sendMessage failed for chat=%s (%s)", chat_id, exc)
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._state.with_lock():
            self._state.poll_state.record_outbound(str(chat_id), now_iso)

    async def send_voice(
        self,
        *,
        chat_ref: str,
        text: str | None = None,
        audio_bytes: bytes | None = None,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a voice note (Session 2 2026-05-16).

        Exactly one of ``text`` / ``audio_bytes`` must be set:
        - ``text`` — synthesised via ``app['tts_engine']`` (whichever lane
          roles.yaml::voice.tts.primary names), then transcoded
          WAV → OGG/Opus via :func:`wav_bytes_to_ogg_opus` so Telegram
          renders the voice-note UI.
        - ``audio_bytes`` — already-encoded OGG/Opus bytes; sent as-is.

        Persists the OGG bytes under ``uploads/channels/`` (direction
        "outbound") so the operator can re-listen later. Appends an
        outbound row to the conversation store. Returns the Telegram
        API's ``sendVoice`` result dict.
        """
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        if (text is None) == (audio_bytes is None):
            raise ValueError("send_voice: pass exactly one of text or audio_bytes")
        chat_id = self._coerce_chat_ref(chat_ref)

        if audio_bytes is None:
            assert text is not None
            engine = self._app.get("tts_engine")
            if engine is None:
                raise RuntimeError("send_voice: no tts_engine available in app context")
            from tesseract.voice.encode import wav_bytes_to_ogg_opus

            wav_bytes, _provider = await engine.synthesize(text, preset="answer")
            if not wav_bytes:
                raise RuntimeError("send_voice: TTS returned empty audio")
            ogg_bytes = await wav_bytes_to_ogg_opus(wav_bytes)
        else:
            ogg_bytes = audio_bytes

        result = await self._api.send_voice(
            chat_id=chat_id,
            ogg_opus_bytes=ogg_bytes,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
        await self._record_outbound_media(
            chat_id=chat_id, kind="voice", data=ogg_bytes,
            filename="voice.ogg", mime_type="audio/ogg",
            caption=caption, body_text=(text or caption or ""),
        )
        return result

    async def send_photo(
        self,
        *,
        chat_ref: str,
        image_bytes: bytes | None = None,
        source_path: str | None = None,
        source_url: str | None = None,
        filename: str = "photo.jpg",
        mime_type: str = "image/jpeg",
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a photo (Session 2 2026-05-16).

        Exactly one source must be set: ``image_bytes`` | ``source_path``
        | ``source_url``. ``source_url`` resolves via httpx (the bridge's
        existing client). ``source_path`` reads from disk — operator-
        owned files only; the kernel-tool layer is responsible for
        validating paths.

        Persists the bytes outbound and appends a conversation-store row.
        """
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        sources_set = sum(s is not None for s in (image_bytes, source_path, source_url))
        if sources_set != 1:
            raise ValueError("send_photo: pass exactly one of image_bytes / source_path / source_url")
        chat_id = self._coerce_chat_ref(chat_ref)

        data = await self._resolve_media_source(
            image_bytes=image_bytes,
            source_path=source_path,
            source_url=source_url,
        )
        result = await self._api.send_photo(
            chat_id=chat_id, image_bytes=data, filename=filename,
            mime_type=mime_type, caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
        await self._record_outbound_media(
            chat_id=chat_id, kind="photo", data=data,
            filename=filename, mime_type=mime_type, caption=caption,
            body_text=caption or "(photo)",
        )
        return result

    async def send_document(
        self,
        *,
        chat_ref: str,
        document_bytes: bytes | None = None,
        source_path: str | None = None,
        filename: str | None = None,
        mime_type: str | None = None,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a document file (Session 2 2026-05-16).

        Source is bytes OR path. ``filename`` is the name the recipient
        sees in their chat client; when omitted on the path branch we
        use ``Path(source_path).name``. ``mime_type`` defaults to
        ``application/octet-stream`` if both omitted and unguessable.
        """
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        if (document_bytes is None) == (source_path is None):
            raise ValueError("send_document: pass exactly one of document_bytes or source_path")
        chat_id = self._coerce_chat_ref(chat_ref)

        from pathlib import Path
        import mimetypes as _mimetypes

        if document_bytes is None:
            assert source_path is not None
            p = Path(source_path)
            if not p.is_file():
                raise FileNotFoundError(f"send_document: source_path not a file: {source_path}")
            data = await asyncio.get_event_loop().run_in_executor(None, p.read_bytes)
            if filename is None:
                filename = p.name
            if mime_type is None:
                guessed, _ = _mimetypes.guess_type(p.name)
                mime_type = guessed or "application/octet-stream"
        else:
            data = document_bytes
            if filename is None:
                filename = "document.bin"
            if mime_type is None:
                guessed, _ = _mimetypes.guess_type(filename)
                mime_type = guessed or "application/octet-stream"

        result = await self._api.send_document(
            chat_id=chat_id, document_bytes=data, filename=filename,
            mime_type=mime_type, caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
        await self._record_outbound_media(
            chat_id=chat_id, kind="document", data=data,
            filename=filename, mime_type=mime_type, caption=caption,
            body_text=caption or f"(document: {filename})",
        )
        return result

    def _coerce_chat_ref(self, chat_ref: str) -> int:
        try:
            return int(chat_ref)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"chat_ref must be an integer chat_id, got {chat_ref!r}"
            ) from exc

    async def _resolve_media_source(
        self,
        *,
        image_bytes: bytes | None,
        source_path: str | None,
        source_url: str | None,
    ) -> bytes:
        if image_bytes is not None:
            return image_bytes
        if source_path is not None:
            from pathlib import Path
            p = Path(source_path)
            if not p.is_file():
                raise FileNotFoundError(f"send_photo: source_path not a file: {source_path}")
            return await asyncio.get_event_loop().run_in_executor(None, p.read_bytes)
        assert source_url is not None
        assert self._api is not None
        try:
            return await self._api.fetch_url(source_url)
        except TelegramAPIError as exc:
            raise RuntimeError(
                f"send_photo: fetch failed for {source_url}: {exc}"
            ) from exc

    async def _record_outbound_media(
        self,
        *,
        chat_id: int,
        kind: str,
        data: bytes,
        filename: str,
        mime_type: str,
        caption: str | None,
        body_text: str,
    ) -> None:
        """Persist outbound media bytes + append the conversation row.

        Best-effort: persistence failure is logged + swallowed so a
        send that already left our process doesn't fail at the
        bookkeeping step (Telegram already delivered the media).
        """
        chat_key = str(chat_id)
        # Use the inbound persistence module; outbound files land in the
        # same tree so the operator's "browse files for this chat" view
        # is symmetric. Message_id is synthetic for outbound (we don't
        # have a Telegram message_id until after the call, and we don't
        # need to dedupe).
        from datetime import datetime as _dt
        synthetic_msg_id = f"out-{_dt.now(timezone.utc).strftime('%H%M%S%f')}"
        storage_path: str | None = None
        try:
            stored = await save_channel_attachment(
                channel=self.name,
                chat_id=chat_key,
                message_id=synthetic_msg_id,
                kind=kind,
                data=data,
                filename=filename,
                mime_type=mime_type,
                source_ref="",
                caption=caption or "",
            )
            storage_path = stored.storage_path
        except Exception:
            log.exception(
                "telegram: failed to persist outbound %s for chat=%s", kind, chat_id,
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self._conversations.append(
                self.name, chat_key,
                ChannelMessage(
                    ts=now_iso, direction="outbound", body=body_text,
                    extra={
                        "kind": kind,
                        "filename": filename,
                        "mime_type": mime_type,
                        "storage_path": storage_path,
                        "caption": caption or "",
                    },
                ),
            )
        except Exception:
            log.exception("telegram: failed to append outbound conversation row")

        with self._state.with_lock():
            self._state.poll_state.messages_out_total[chat_key] = (
                self._state.poll_state.messages_out_total.get(chat_key, 0) + 1
            )
            self._state.poll_state.record_outbound(chat_key, now_iso)
            save_state(self._state.state_path, self._state.poll_state)

    async def send_video(
        self,
        *,
        chat_ref: str,
        video_bytes: bytes | None = None,
        source_path: str | None = None,
        source_url: str | None = None,
        filename: str = "video.mp4",
        mime_type: str = "video/mp4",
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a video (Session 3 2026-05-16). Source: bytes | path | url."""
        return await self._send_media_payload(
            chat_ref=chat_ref, kind="video",
            data_bytes=video_bytes, source_path=source_path, source_url=source_url,
            filename=filename, mime_type=mime_type, caption=caption,
            reply_to_message_id=reply_to_message_id,
            api_call=lambda data, fn: self._api.send_video(
                chat_id=self._coerce_chat_ref(chat_ref),
                video_bytes=data, filename=fn, mime_type=mime_type,
                caption=caption, reply_to_message_id=reply_to_message_id,
            ),
        )

    async def send_animation(
        self,
        *,
        chat_ref: str,
        animation_bytes: bytes | None = None,
        source_path: str | None = None,
        source_url: str | None = None,
        filename: str = "animation.mp4",
        mime_type: str = "video/mp4",
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a GIF / animation (Session 3 2026-05-16)."""
        return await self._send_media_payload(
            chat_ref=chat_ref, kind="animation",
            data_bytes=animation_bytes, source_path=source_path, source_url=source_url,
            filename=filename, mime_type=mime_type, caption=caption,
            reply_to_message_id=reply_to_message_id,
            api_call=lambda data, fn: self._api.send_animation(
                chat_id=self._coerce_chat_ref(chat_ref),
                animation_bytes=data, filename=fn, mime_type=mime_type,
                caption=caption, reply_to_message_id=reply_to_message_id,
            ),
        )

    async def send_video_note(
        self,
        *,
        chat_ref: str,
        video_bytes: bytes | None = None,
        source_path: str | None = None,
        source_url: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a round video note (Session 3 2026-05-16). No caption."""
        return await self._send_media_payload(
            chat_ref=chat_ref, kind="video_note",
            data_bytes=video_bytes, source_path=source_path, source_url=source_url,
            filename="video_note.mp4", mime_type="video/mp4", caption=None,
            reply_to_message_id=reply_to_message_id,
            api_call=lambda data, fn: self._api.send_video_note(
                chat_id=self._coerce_chat_ref(chat_ref),
                video_bytes=data, filename=fn,
                reply_to_message_id=reply_to_message_id,
            ),
        )

    async def send_sticker(
        self,
        *,
        chat_ref: str,
        sticker: str | bytes,
        emoji: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a sticker — either a Telegram file_id or raw WebP bytes
        (Session 3 2026-05-16)."""
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        chat_id = self._coerce_chat_ref(chat_ref)
        result = await self._api.send_sticker(
            chat_id=chat_id, sticker=sticker, emoji=emoji,
            reply_to_message_id=reply_to_message_id,
        )
        body_text = f"(sticker {emoji or ''})".strip()
        if isinstance(sticker, bytes):
            await self._record_outbound_media(
                chat_id=chat_id, kind="sticker", data=sticker,
                filename="sticker.webp", mime_type="image/webp",
                caption=emoji, body_text=body_text,
            )
        else:
            self._record_outbound_text_event(chat_id, body_text)
        return result

    async def send_location(
        self,
        *,
        chat_ref: str,
        latitude: float,
        longitude: float,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Share a static location (Session 3 2026-05-16)."""
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        chat_id = self._coerce_chat_ref(chat_ref)
        result = await self._api.send_location(
            chat_id=chat_id, latitude=latitude, longitude=longitude,
            reply_to_message_id=reply_to_message_id,
        )
        self._record_outbound_text_event(
            chat_id, f"(location {latitude:.4f},{longitude:.4f})",
        )
        return result

    async def send_contact(
        self,
        *,
        chat_ref: str,
        phone_number: str,
        first_name: str,
        last_name: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Share a contact card (Session 3 2026-05-16)."""
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        chat_id = self._coerce_chat_ref(chat_ref)
        result = await self._api.send_contact(
            chat_id=chat_id, phone_number=phone_number,
            first_name=first_name, last_name=last_name,
            reply_to_message_id=reply_to_message_id,
        )
        label = f"{first_name}{(' ' + last_name) if last_name else ''}"
        self._record_outbound_text_event(chat_id, f"(contact {label} {phone_number})")
        return result

    async def send_poll(
        self,
        *,
        chat_ref: str,
        question: str,
        options: list[str],
        is_anonymous: bool = True,
        allows_multiple_answers: bool = False,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a poll (Session 3 2026-05-16)."""
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        chat_id = self._coerce_chat_ref(chat_ref)
        result = await self._api.send_poll(
            chat_id=chat_id, question=question, options=options,
            is_anonymous=is_anonymous,
            allows_multiple_answers=allows_multiple_answers,
            reply_to_message_id=reply_to_message_id,
        )
        self._record_outbound_text_event(
            chat_id, f"(poll: {question} — {len(options)} options)",
        )
        return result

    async def send_dice(
        self,
        *,
        chat_ref: str,
        emoji: str = "🎲",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Send an animated dice / game (Session 3 2026-05-16)."""
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        chat_id = self._coerce_chat_ref(chat_ref)
        result = await self._api.send_dice(
            chat_id=chat_id, emoji=emoji,
            reply_to_message_id=reply_to_message_id,
        )
        self._record_outbound_text_event(chat_id, f"(dice {emoji})")
        return result

    async def send_media_group(
        self,
        *,
        chat_ref: str,
        media: list[dict[str, Any]],
        reply_to_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Send an album of 2-10 photos/videos in one bubble (Session 3 2026-05-16)."""
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        chat_id = self._coerce_chat_ref(chat_ref)
        result = await self._api.send_media_group(
            chat_id=chat_id, media=media,
            reply_to_message_id=reply_to_message_id,
        )
        self._record_outbound_text_event(
            chat_id, f"(album with {len(media)} items)",
        )
        return result

    async def _send_media_payload(
        self,
        *,
        chat_ref: str,
        kind: str,
        data_bytes: bytes | None,
        source_path: str | None,
        source_url: str | None,
        filename: str,
        mime_type: str,
        caption: str | None,
        reply_to_message_id: int | None,
        api_call,
    ) -> dict[str, Any]:
        """Shared resolve+upload+persist path for video/animation/video_note.

        Exactly one source must be set. Reads from disk via the executor
        when path-based, fetches via :meth:`TelegramAPI.fetch_url` when
        URL-based. Persists outbound bytes mirroring the photo/document
        layout so the operator can re-open the file later.
        """
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        sources_set = sum(s is not None for s in (data_bytes, source_path, source_url))
        if sources_set != 1:
            raise ValueError(
                f"send_{kind}: pass exactly one of data_bytes / source_path / source_url"
            )
        chat_id = self._coerce_chat_ref(chat_ref)
        data = await self._resolve_media_source(
            image_bytes=data_bytes, source_path=source_path, source_url=source_url,
        )
        # If source_path was set and filename is the default, derive the
        # display filename from the actual path so the recipient sees a
        # meaningful name.
        if source_path is not None and filename in ("video.mp4", "animation.mp4"):
            from pathlib import Path
            p = Path(source_path)
            if p.name:
                filename = p.name
        result = await api_call(data, filename)
        await self._record_outbound_media(
            chat_id=chat_id, kind=kind, data=data,
            filename=filename, mime_type=mime_type, caption=caption,
            body_text=caption or f"({kind}: {filename})",
        )
        return result

    def _record_outbound_text_event(self, chat_id: int, body_text: str) -> None:
        """Append a synthetic outbound conversation row for non-file
        outbounds (location/poll/dice/file_id-sticker) so the chat
        history shows the assistant sent something. Stamps the rolling 24h
        counters too.
        """
        chat_key = str(chat_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self._conversations.append(
                self.name, chat_key,
                ChannelMessage(
                    ts=now_iso, direction="outbound", body=body_text, extra={},
                ),
            )
        except Exception:
            log.exception("telegram: failed to append outbound text-event row")
        with self._state.with_lock():
            self._state.poll_state.messages_out_total[chat_key] = (
                self._state.poll_state.messages_out_total.get(chat_key, 0) + 1
            )
            self._state.poll_state.record_outbound(chat_key, now_iso)
            save_state(self._state.state_path, self._state.poll_state)

    async def send_text(
        self, *, chat_ref: str, text: str,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Public adapter surface for fire-and-forget outbound text.

        Used by the daily-brief push (MO-10-3) and any future "send a
        notification to a chat outside the normal turn loop" caller.
        Long bodies are split via :func:`chunk_for_telegram` so brief
        summaries that grow past 4000 chars no longer silently truncate
        (audit fix M2). HTML→plain fallback preserved per chunk.

        ``reply_to_message_id`` (Session 2 2026-05-16) — attached to the
        first chunk only so the quote-reply doesn't repeat across the
        thread.
        """
        if self._api is None:
            raise RuntimeError("telegram bridge not initialized")
        try:
            chat_id = int(chat_ref)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"chat_ref must be an integer chat_id, got {chat_ref!r}") from exc
        chunks = chunk_for_telegram(text or "")
        if not chunks:
            return
        for idx, chunk in enumerate(chunks):
            # ``send_text`` may receive pre-rendered Telegram-HTML (daily
            # brief, command router) or plain text. Either way the HTML
            # attempt is safe — Telegram accepts a plain string under
            # ``parse_mode="HTML"``. The plain fallback must strip the
            # markup so a parse failure does not ship literal ``<b>``
            # tags to the phone (reviewer P0-2).
            sent = await self._send_fresh(
                chat_id=chat_id,
                html=chunk,
                plain=_strip_html_tags(chunk),
                reply_to_message_id=(reply_to_message_id if idx == 0 else None),
            )
            if not sent:
                return
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._state.with_lock():
            self._state.poll_state.record_outbound(str(chat_id), now_iso)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    # -- ChannelAdapter surface -------------------------------------------

    def status_snapshot(self) -> ChannelStatus:
        # ``_bridge_phase`` is the source of truth (set by
        # ``_supervised_loop``): ``stopped`` (no task) → ``starting``
        # (getMe retry loop) → ``running`` (in poll loop). The
        # task-aliveness check below is a safety net — if the
        # supervisor crashed unexpectedly, surface ``stopped`` even if
        # the phase attribute wasn't reset.
        # Fixture-only tests build the bridge via ``__new__`` and skip
        # ``__init__``, so the phase attrs may be missing — fall back
        # to "stopped" defensively rather than crashing the snapshot.
        last_getme_error = getattr(self, "_last_getme_error", None)
        bridge_phase = getattr(self, "_bridge_phase", "stopped")
        if self._poll_task is None or self._poll_task.done():
            bridge_state = "stopped"
            if last_getme_error:
                bridge_state = "error"
        else:
            bridge_state = bridge_phase
        # m1 — actual rolling 24h totals (was: lifetime sums). Pruning
        # happens lazily inside the count helpers so the read is cheap
        # and never returns stale entries.
        messages_in_24h = self._state.poll_state.count_inbound_24h()
        messages_out_24h = self._state.poll_state.count_outbound_24h()
        return ChannelStatus(
            name=self.name,
            bridge_state=bridge_state,  # type: ignore[arg-type]
            last_poll_at=self._last_poll_at,
            error_count_24h=self._error_count,
            messages_in_24h=messages_in_24h,
            messages_out_24h=messages_out_24h,
            pending_count=len(self._state.allowlist.pending),
            allowed_count=len(self._state.allowlist.chat_ids),
        )

    def list_users(self) -> list[ChannelUser]:
        out: list[ChannelUser] = []
        for chat_id in sorted(self._state.allowlist.chat_ids):
            out.append(self._project_user(chat_id, state="allowed"))
        for chat_id in sorted(self._state.allowlist.pending.keys()):
            out.append(self._project_user(chat_id, state="pending"))
        for chat_id in sorted(self._state.allowlist.blocked):
            out.append(self._project_user(chat_id, state="blocked"))
        return out

    def _project_user(self, chat_id: int, *, state: str) -> ChannelUser:
        key = str(chat_id)
        pending = self._state.allowlist.pending.get(chat_id)
        display = self._state.poll_state.user_display.get(key)
        if display is None and pending is not None and pending.username:
            display = f"@{pending.username}"
        if not display:
            display = f"chat:{chat_id}"
        tier = self._state.poll_state.user_tier.get(key, "operator")
        ttl = self._state.poll_state.user_ttl.get(key) or None
        first_seen = (
            self._state.poll_state.first_seen.get(key)
            or (pending.first_seen if pending is not None else "")
        )
        last_seen = self._state.poll_state.last_message_ts.get(key, first_seen)
        messages_total = (
            self._state.poll_state.messages_in_total.get(key, 0)
            + self._state.poll_state.messages_out_total.get(key, 0)
        )
        return ChannelUser(
            user_id=key,
            display_name=display,
            tier=tier,  # type: ignore[arg-type]
            ttl_iso=ttl,
            first_seen=first_seen,
            last_seen=last_seen,
            messages_total=messages_total,
            state=state,  # type: ignore[arg-type]
        )

    async def approve(
        self,
        user_id: str,
        *,
        tier: ChannelUserTier,
        ttl_iso: str | None,
        display_name: str | None,
    ) -> ChannelUser:
        chat_id = _coerce_chat_id(user_id)
        key = str(chat_id)
        with self._state.with_lock():
            self._state.allowlist.chat_ids.add(chat_id)
            self._state.allowlist.pending.pop(chat_id, None)
            self._state.allowlist.blocked.discard(chat_id)
            save_allowlist(self._state.allowlist_path, self._state.allowlist)
            self._state.poll_state.user_tier[key] = tier
            if ttl_iso:
                self._state.poll_state.user_ttl[key] = ttl_iso
            else:
                self._state.poll_state.user_ttl.pop(key, None)
            if display_name:
                self._state.poll_state.user_display[key] = display_name
            self._state.poll_state.first_seen.setdefault(
                key, datetime.now(timezone.utc).isoformat()
            )
            save_state(self._state.state_path, self._state.poll_state)
        return self._project_user(chat_id, state="allowed")

    async def revoke(self, user_id: str) -> ChannelUser:
        chat_id = _coerce_chat_id(user_id)
        with self._state.with_lock():
            self._state.allowlist.chat_ids.discard(chat_id)
            save_allowlist(self._state.allowlist_path, self._state.allowlist)
        # Drop the chat's in-memory session so the next message (if the
        # operator re-approves) starts cleanly.
        session = self._sessions.pop(chat_id, None)
        if session is not None:
            await self._cancel_session_turn(session)
        return self._project_user(chat_id, state="pending")

    async def block(self, user_id: str) -> ChannelUser:
        chat_id = _coerce_chat_id(user_id)
        with self._state.with_lock():
            self._state.allowlist.blocked.add(chat_id)
            self._state.allowlist.chat_ids.discard(chat_id)
            self._state.allowlist.pending.pop(chat_id, None)
            save_allowlist(self._state.allowlist_path, self._state.allowlist)
        session = self._sessions.pop(chat_id, None)
        if session is not None:
            await self._cancel_session_turn(session)
        return self._project_user(chat_id, state="blocked")

    def list_conversation(
        self,
        user_id: str,
        *,
        limit: int = 100,
        before_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        chat_id = _coerce_chat_id(user_id)
        return self._conversations.tail(
            self.name,
            str(chat_id),
            limit=limit,
            before_iso=before_iso,
        )


async def _cancel_task(task: asyncio.Task | None) -> None:
    """Cancel a background task and wait for unwind (swallowed cancel)."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.debug("telegram: background task error during cancel", exc_info=True)


def _strip_html_tags(text: str) -> str:
    """Remove Telegram-HTML tags so a plain-mode fallback renders cleanly.

    Telegram's HTML subset is narrow (``<b>`` ``<i>`` ``<u>`` ``<s>``
    ``<code>`` ``<pre>`` ``<a>`` ``<tg-spoiler>``); we drop all of them
    via a single ``<[^>]+>`` regex. Unescape ``&amp;`` ``&lt;`` ``&gt;``
    back so the operator's "&" in a brief title shows as "&" once HTML
    parsing fails, not as ``&amp;``.
    """
    import re
    from html import unescape

    stripped = re.sub(r"<[^>]+>", "", text)
    return unescape(stripped)


# `/clear` confirmation tokens (2026-05-16). Lowercased; the bridge's
# follow-up handler casefolds the incoming body before comparing.
_CLEAR_YES_TOKENS: frozenset[str] = frozenset({"yes", "y", "sure", "ok", "👍"})
_CLEAR_NO_TOKENS: frozenset[str] = frozenset({"no", "n", "nope", "skip", "👎"})
_CLEAR_PENDING_TTL_S = 300.0


def _pending_clear_expired(stamp_iso: str) -> bool:
    """Return True when a /clear stamp is older than _CLEAR_PENDING_TTL_S.

    Malformed timestamps are treated as expired so a corrupted state
    file errs on the side of "cancel the pending stamp" rather than
    locking the chat indefinitely.
    """
    try:
        stamped = datetime.fromisoformat(stamp_iso)
    except (TypeError, ValueError):
        return True
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - stamped).total_seconds()
    return age > _CLEAR_PENDING_TTL_S


def _format_gated_footer(n: int) -> str:
    """Friendly "switch to Mirror" message for n workspace-gated calls (A1)."""
    if n == 1:
        return (
            "(I paused on one action that needs your approval — open Mirror "
            "to resume.)"
        )
    return (
        f"(I paused on {n} actions that need your approval — open Mirror to "
        "resume.)"
    )


def _fetch_failure_to_status(kind: str) -> str:
    """Map ``FetchRejection.kind`` to a ``ChannelAttachmentStatus``; ``missing_ref``/``fetch_failed`` collapse to ``extract_failed``."""
    if kind == "too_large":
        return "too_large"
    return "extract_failed"


def _coerce_chat_id(user_id: str) -> int:
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"telegram: user_id must be an integer chat_id, got {user_id!r}") from exc


def build_telegram_bridge(
    app: web.Application,
    *,
    conversation_store: ConversationStore | None = None,
) -> TelegramBridge | None:
    # The variable's NAME comes from the channel's own block, so
    # `channels.yaml::telegram.api_key_env` is the authority rather than a
    # declaration this line happens to agree with.
    key_env = channel_key_env("telegram")
    token = (os.environ.get(key_env) or "").strip()
    if not token:
        log.info("telegram: %s not set; bridge disabled", key_env)
        return None
    store = conversation_store or app.get("conversation_store") or ConversationStore()
    chat_memory = ChatMemoryService(
        conversation_store=store,
        memory_bundle=app.get("memory_bundle"),
    )
    return TelegramBridge(
        token=token,
        app=app,
        conversation_store=store,
        env_seed_chat_ids=os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"),
        chat_memory=chat_memory,
    )
