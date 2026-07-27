"""IPC client used by the ``tars`` terminal client + X-4 lane bridge.

Connects to a running :class:`ControllerDaemon` over loopback TCP,
performs the token handshake, and exposes a high-level send / receive
API that mirrors the message vocabulary in `_shared/ipc-contract.md`.

Use as an async context manager::

    async with ControllerClient.connect() as client:
        sessions = await client.list_sessions()
        attached = await client.attach(sessions[0]["session_id"])
        async for push in client.pushes():
            ...

The client is *long-lived* — TC-5's `reload_bridge` does a one-shot
connect-send-close because the Mirror watcher cannot keep a TCP socket
open across reloads. TC-6 keeps the socket alive for the lifetime of
the TUI session because the renderer streams `transcript_event`
pushes in real time.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, AsyncIterator

from tesseract.kernel.sandbox._ipc_frames import decode_frame, encode_frame

from . import auth as controller_auth
from .paths import port_file_path

log = logging.getLogger(__name__)


class ControllerClientError(RuntimeError):
    """Raised when the IPC handshake or transport hits an
    unrecoverable error before the TUI ever attaches."""


class ControllerClient:
    """Async client for the controller daemon's IPC endpoint.

    The class owns the asyncio reader/writer pair and a queue of pushes
    that arrive while no caller is awaiting them. Single-consumer:
    only one task should iterate :meth:`pushes` at a time.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        token: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._token = token
        self._closed = False
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        # Reply demultiplexer (2026-05-24 fix). A long-running `pushes()`
        # consumer and a request/reply `_await_event` call BOTH used to
        # pull from `_inbox`, so a reply (`session_list`, `attached`,
        # `reload_complete`, …) could be grabbed by the push loop and
        # silently dropped — that broke `/sessions`, `/new`, `/delete`,
        # `/title`, `/reload` in the Textual TUI which runs `pushes()`
        # permanently. Request/reply calls now register a Future here;
        # the reader resolves the matching reply directly and only
        # un-awaited events reach `_inbox`/`pushes()`.
        self._reply_waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        # `lane_result` pushes are keyed by request_id, not event name.
        self._lane_request_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Set once the reader loop exits (EOF / disconnect) so a request
        # issued afterwards fails fast instead of waiting out its timeout.
        self._reader_done = False

    # ── construction ──────────────────────────────────────────────────

    @classmethod
    async def connect(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        token: str | None = None,
        connect_timeout: float = 2.0,
    ) -> "ControllerClient":
        """Resolve port + token from disk if not provided, open the TCP
        connection, authenticate. Raises
        :class:`ControllerClientError` on any failure before auth
        completes — callers surface that as "no controller running"."""
        if port is None:
            path = port_file_path()
            if not path.exists():
                raise ControllerClientError(
                    f"no controller port file at {path}; is the daemon running?"
                )
            try:
                port = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError) as exc:
                raise ControllerClientError(
                    f"controller port file unreadable: {exc}"
                ) from exc
        if token is None:
            token = controller_auth.read_token()
            if not token:
                raise ControllerClientError(
                    "no controller token on disk; daemon may not have minted one"
                )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=connect_timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ControllerClientError(
                f"controller connect failed: {exc}"
            ) from exc

        # Send auth handshake before spawning the reader task — the
        # daemon's reply (or close) determines whether we proceed.
        writer.write(encode_frame({"auth": token}))
        try:
            await writer.drain()
        except OSError as exc:
            raise ControllerClientError(f"auth write failed: {exc}") from exc

        client = cls(reader, writer, token)
        # Probe one byte ahead to see if the daemon closed the socket
        # immediately (bad token) — but DON'T read the line because the
        # daemon sends nothing on success. Instead start the reader
        # task and let the first IPC call hit a closed-connection error
        # if the handshake was rejected.
        client._start_reader_task()
        return client

    # ── lifecycle ─────────────────────────────────────────────────────

    def _start_reader_task(self) -> None:
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="controller-client-reader"
            )

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    payload = await decode_frame(self._reader)
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                except ValueError as exc:  # oversize / malformed frame
                    log.error(
                        "controller_client: oversize frame, closing: %s", exc
                    )
                    return
                if self._resolve_lane_result(payload):
                    continue
                if self._resolve_reply(payload):
                    continue
                await self._inbox.put(payload)
        finally:
            self._reader_done = True
            # Wake any in-flight request waiters so they don't hang.
            self._fail_all_waiters(ControllerClientError("controller disconnected"))
            # Sentinel so a consumer awaiting pushes() wakes up and exits.
            await self._inbox.put({"event": "_disconnected"})

    def _resolve_reply(self, payload: dict[str, Any]) -> bool:
        """Route a reply payload to a pending request waiter, if any.

        Returns True when the payload was consumed by a waiter (so it
        must NOT also reach `_inbox`/`pushes()`). An ``error`` push is
        treated as a reply ONLY when a request is in-flight (a waiter is
        registered for ``error``); otherwise it falls through to the
        push loop as an out-of-band error.
        """
        event = payload.get("event")
        if not isinstance(event, str):
            return False
        waiters = self._reply_waiters.get(event)
        if not waiters:
            return False
        fut = waiters.pop(0)
        if not waiters:
            self._reply_waiters.pop(event, None)
        if not fut.done():
            fut.set_result(payload)
        return True

    def _fail_all_waiters(self, exc: Exception) -> None:
        for waiters in list(self._reply_waiters.values()):
            for fut in waiters:
                if not fut.done():
                    fut.set_exception(exc)
        self._reply_waiters.clear()
        for fut in list(self._lane_request_waiters.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._lane_request_waiters.clear()

    def _resolve_lane_result(self, payload: dict[str, Any]) -> bool:
        """Route a `lane_result` push to the waiter keyed by `request_id`.
        Returns True when consumed so the payload doesn't double-land
        in `_inbox` / `pushes()` or in the legacy event-name waiter."""
        if payload.get("event") != "lane_result":
            return False
        rid = payload.get("request_id")
        if not isinstance(rid, str):
            return False
        fut = self._lane_request_waiters.pop(rid, None)
        if fut is None:
            return False
        if not fut.done():
            fut.set_result(payload)
        return True

    def _register_waiter(
        self, event_name: str
    ) -> asyncio.Future[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        if self._reader_done or self._closed:
            # Reader already gone — fail fast rather than wait out the
            # caller's timeout for a reply that can never arrive.
            fut.set_exception(ControllerClientError("controller disconnected"))
            return fut
        self._reply_waiters.setdefault(event_name, []).append(fut)
        return fut

    def _drop_waiter(
        self, event_name: str, fut: asyncio.Future[dict[str, Any]]
    ) -> None:
        waiters = self._reply_waiters.get(event_name)
        if not waiters:
            return
        try:
            waiters.remove(fut)
        except ValueError:
            pass
        if not waiters:
            self._reply_waiters.pop(event_name, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader_task.cancel()

    async def __aenter__(self) -> "ControllerClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── transport ─────────────────────────────────────────────────────

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._closed:
            raise ControllerClientError("client closed")
        try:
            self._writer.write(encode_frame(payload))
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            raise ControllerClientError(f"send failed: {exc}") from exc

    async def _await_event_or_error(
        self,
        event_name: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Like :meth:`_await_event` but also resolves on an ``error``
        push — surfaces the error as :class:`ControllerClientError` so
        callers don't time out waiting for a success event that the
        daemon already refused. Reply demultiplexing keeps this immune
        to a concurrent ``pushes()`` consumer (see ``_resolve_reply``).
        """
        ok_fut = self._register_waiter(event_name)
        err_fut = self._register_waiter("error")
        try:
            done, _ = await asyncio.wait(
                {ok_fut, err_fut},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise ControllerClientError(
                    f"timed out waiting for {event_name}"
                )
            # Disconnect fast-fail / reader EOF sets an exception on the
            # waiter(s) — surface it directly.
            for fut in (ok_fut, err_fut):
                if fut in done and not fut.cancelled() and fut.exception():
                    raise fut.exception()  # type: ignore[misc]
            if ok_fut in done:
                # Success. If an *unrelated* error push happened to land
                # in the same wait tick, it was consumed by our err
                # waiter — hand it back to the push loop so it isn't
                # silently swallowed (reviewer-flagged race).
                if err_fut in done and not err_fut.cancelled():
                    try:
                        self._inbox.put_nowait(err_fut.result())
                    except asyncio.QueueFull:  # pragma: no cover
                        pass
                return ok_fut.result()
            payload = err_fut.result()
            code = payload.get("code") or "error"
            detail = payload.get("detail") or ""
            raise ControllerClientError(f"{code}: {detail}")
        finally:
            self._drop_waiter(event_name, ok_fut)
            self._drop_waiter("error", err_fut)

    async def _await_event(
        self, event_name: str, *, timeout: float = 10.0
    ) -> dict[str, Any]:
        """Await a single request/reply event by name. The reader routes
        the matching reply straight to our Future (``_resolve_reply``),
        so a concurrent ``pushes()`` loop can't swallow it. Used by
        request/response IPC calls whose reply shape is known."""
        fut = self._register_waiter(event_name)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ControllerClientError(
                f"timed out waiting for {event_name}"
            ) from exc
        finally:
            self._drop_waiter(event_name, fut)

    # ── high-level API ────────────────────────────────────────────────

    async def list_sessions(self) -> list[dict[str, Any]]:
        await self._send({"msg": "list_sessions"})
        reply = await self._await_event("session_list")
        return list(reply.get("sessions") or [])

    async def new_session(
        self,
        *,
        title: str | None = None,
        mode: str = "chat",
        origin: str = "cli",
        preferred_coder: str | None = None,
    ) -> dict[str, Any]:
        await self._send(
            {
                "msg": "new_session",
                "title": title,
                "mode": mode,
                "origin": origin,
                "preferred_coder": preferred_coder,
            }
        )
        reply = await self._await_event("attached")
        return reply

    async def attach(
        self,
        session_id: str,
        *,
        mode: str = "interactive",
        from_offset: int = 0,
    ) -> dict[str, Any]:
        await self._send(
            {
                "msg": "attach",
                "session_id": session_id,
                "mode": mode,
                "from_offset": from_offset,
            }
        )
        return await self._await_event("attached")

    async def user_input(
        self, session_id: str, text: str, *, await_ack: bool = False
    ) -> None:
        """Send operator text into a controller session.

        ``await_ack=True`` (M9) waits for the daemon's ``ack`` and raises
        ``ControllerClientError`` on ``session_not_found``/timeout, so a caller
        with no live transcript to observe (``work_send``) can't report a false
        success for a stale session id. Interactive attach paths that watch the
        transcript stream leave it ``False`` (fire-and-forget)."""
        await self._send(
            {"msg": "user_input", "session_id": session_id, "text": text}
        )
        if await_ack:
            await self._await_event_or_error("ack")

    async def approval(
        self,
        session_id: str,
        tool_use_id: str,
        approved: bool,
        operator_note: str | None = None,
    ) -> None:
        await self._send(
            {
                "msg": "approval",
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "approved": approved,
                "operator_note": operator_note,
            }
        )

    async def cancel_worker(self, session_id: str, worker_id: str) -> None:
        await self._send(
            {
                "msg": "cancel_worker",
                "session_id": session_id,
                "worker_id": worker_id,
            }
        )

    async def detach(self, session_id: str) -> None:
        await self._send({"msg": "detach", "session_id": session_id})

    async def request_snapshot(self) -> None:
        """AS-1 gap-a — ask the controller for a full Activity-registry
        snapshot. Fire-and-forget: the daemon replies with an
        ``activity_snapshot`` push that arrives on :meth:`pushes` (it is not a
        request/reply `_await_event` — the long-lived subscriber consumes it in
        its push loop, same path as ``activity_event``)."""
        await self._send({"msg": "activity_snapshot"})

    async def request_parked_asks_snapshot(self) -> None:
        """Option B (2026-07-13) — ask the controller for a full snapshot of
        its parked-asks set. Fire-and-forget, mirroring
        :meth:`request_snapshot`: the daemon replies with a
        ``parked_asks_snapshot`` push that arrives on :meth:`pushes` (the
        long-lived Mirror subscriber consumes it there, not via
        ``_await_event``)."""
        await self._send({"msg": "parked_asks_snapshot"})

    async def decide_parked_ask(
        self,
        approval_id: str,
        approved: bool,
        note: str = "",
    ) -> dict[str, Any]:
        """Settle a controller-side parked ask (Option B). Raises
        :class:`ControllerClientError` if ``approval_id`` is unknown or
        already settled. Returns the daemon's ``ack`` payload on success.
        """
        await self._send(
            {
                "msg": "decide_parked_ask",
                "approval_id": approval_id,
                "approved": approved,
                "operator_note": note or None,
            }
        )
        return await self._await_event_or_error("ack")

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        """Delete a session record + transcript. Raises
        :class:`ControllerClientError` if the daemon refuses (session
        attached, missing, etc.). Returns the
        :class:`SessionDeletedPush` payload on success.
        """
        await self._send(
            {"msg": "delete_session", "session_id": session_id}
        )
        return await self._await_event_or_error("session_deleted")

    async def rename_session(
        self, session_id: str, title: str
    ) -> dict[str, Any]:
        """Rename a session's title in the registry. Raises
        :class:`ControllerClientError` on failure. Returns the
        :class:`SessionRenamedPush` payload on success.
        """
        await self._send(
            {
                "msg": "rename_session",
                "session_id": session_id,
                "title": title,
            }
        )
        return await self._await_event_or_error("session_renamed")

    async def reload(self, target: str = "all") -> dict[str, Any]:
        """Drain in-flight turns then reload runtime config. Returns
        the :class:`ReloadCompletePush` payload.
        """
        await self._send({"msg": "reload", "target": target})
        return await self._await_event_or_error(
            "reload_complete", timeout=60.0
        )

    async def shutdown(self) -> None:
        """Tell the daemon to exit. The daemon ACKs then tears down —
        the next ``ControllerClient.connect()`` from any process will
        spawn a fresh daemon. Used by ``tars`` on default Ctrl+C exit
        so code edits take effect without a manual taskkill.
        """
        await self._send({"msg": "shutdown"})

    # ── lane.* (X-4 Session C) ────────────────────────────────────────

    async def _lane_call(
        self,
        msg: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a lane.* message + await the matching `lane_result` push.

        The push is an accept-ack ("queued"), never turn completion —
        turns are awaited via the `turn_ended` event stream. `timeout`
        defaults to cockpit.yaml conductor.lane_ack_timeout_s (2026-07-13
        incident: a hardcoded 30 s here + a send that ran the whole turn
        inline falsely failed every lane turn longer than 30 s).

        Raises :class:`ControllerClientError` on non-ok results or
        transport failure. Returns the `result` dict from the daemon."""
        if timeout is None:
            from tesseract.config.cockpit import load_lane_ack_timeout_s

            timeout = load_lane_ack_timeout_s()
        request_id = secrets.token_hex(8)
        msg = {**msg, "request_id": request_id}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        if self._reader_done or self._closed:
            raise ControllerClientError("controller disconnected")
        self._lane_request_waiters[request_id] = fut
        try:
            await self._send(msg)
            payload = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ControllerClientError(
                f"timed out waiting for lane_result (msg={msg.get('msg')})"
            ) from exc
        finally:
            # `finally` catches `CancelledError` too — `asyncio.CancelledError`
            # is `BaseException`, not `Exception`, in 3.12, so an outer
            # `task.cancel()` would otherwise leave a stale waiter entry
            # behind for the lifetime of the connection.
            self._lane_request_waiters.pop(request_id, None)
        if not payload.get("ok"):
            err = payload.get("error") or "lane operation failed"
            raise ControllerClientError(f"{msg.get('msg')}: {err}")
        result = payload.get("result")
        return dict(result) if isinstance(result, dict) else {}

    async def lane_open(
        self,
        *,
        kind: str,
        mode: str = "headless",
        model: str,
        working_dir: str,
        env: dict[str, str] | None = None,
    ) -> str:
        payload = await self._lane_call(
            {
                "msg": "lane_open",
                "kind": kind,
                "mode": mode,
                "model": model,
                "working_dir": working_dir,
                "env": env,
            }
        )
        return str(payload.get("lane_id") or "")

    async def lane_send(self, lane_id: str, message: str) -> dict[str, Any]:
        return await self._lane_call(
            {"msg": "lane_send", "lane_id": lane_id, "message": message}
        )

    async def lane_read(
        self,
        lane_id: str,
        since_cursor: str | None = None,
    ) -> dict[str, Any]:
        return await self._lane_call(
            {
                "msg": "lane_read",
                "lane_id": lane_id,
                "since_cursor": since_cursor,
            }
        )

    async def lane_status(self, lane_id: str) -> dict[str, Any]:
        return await self._lane_call(
            {"msg": "lane_status", "lane_id": lane_id}
        )

    async def lane_attach(self, lane_id: str) -> dict[str, Any]:
        return await self._lane_call(
            {"msg": "lane_attach", "lane_id": lane_id}
        )

    async def lane_close(
        self,
        lane_id: str,
        reason: str = "operator_close",
    ) -> dict[str, Any]:
        return await self._lane_call(
            {"msg": "lane_close", "lane_id": lane_id, "reason": reason}
        )

    async def lane_interrupt(self, lane_id: str) -> dict[str, Any]:
        # M2 — cancel the lane's in-flight turn (steer) without closing it.
        return await self._lane_call(
            {"msg": "lane_interrupt", "lane_id": lane_id}
        )

    async def lane_list(self) -> list[str]:
        payload = await self._lane_call({"msg": "lane_list"})
        ids = payload.get("ids") or []
        return [str(x) for x in ids]

    # ── named lanes (CV-1) ────────────────────────────────────────────

    async def lane_named_ensure(
        self,
        *,
        name: str,
        kind: str,
        model: str,
        working_dir: str,
        mode: str = "headless",
    ) -> dict[str, Any]:
        """Ensure a named lane exists; returns the NamedLaneRecord dict."""
        return await self._lane_call(
            {
                "msg": "lane_named_ensure",
                "name": name,
                "kind": kind,
                "model": model,
                "working_dir": working_dir,
                "mode": mode,
            }
        )

    async def lane_named_get(self, name: str) -> dict[str, Any] | None:
        """Return the NamedLaneRecord dict for ``name``, or None if unbound."""
        payload = await self._lane_call(
            {"msg": "lane_named_get", "name": name}
        )
        record = payload.get("record")
        return dict(record) if isinstance(record, dict) else None

    async def lane_named_list(self) -> list[dict[str, Any]]:
        payload = await self._lane_call({"msg": "lane_named_list"})
        records = payload.get("records") or []
        return [dict(r) for r in records if isinstance(r, dict)]

    async def pushes(self) -> AsyncIterator[dict[str, Any]]:
        """Yield push payloads as they arrive. Stops when the
        connection closes (yields the internal `_disconnected` sentinel
        once then returns)."""
        while not self._closed:
            payload = await self._inbox.get()
            if payload.get("event") == "_disconnected":
                # Surface the close to the caller and stop.
                yield payload
                return
            yield payload


__all__ = ["ControllerClient", "ControllerClientError"]
