"""TC-6 — `agent` terminal client entry point.

Operator types `agent` in any shell; the script connects to the running
controller daemon, picks (or creates) a session, replays the
transcript, and streams new events live. ``Ctrl-C`` cancels the active
child worker; a second ``Ctrl-C`` detaches and exits.

Argument set (covers the phase-doc UX):

* `agent`                   — interactive picker over active sessions
* `agent --new`             — mint a new session and attach
* `agent --session <id>`    — attach to a specific id
* `agent --list`            — list active sessions and exit

The script is intentionally thin: protocol lives in
``ipc_client.ControllerClient`` and rendering lives in
``renderer.TuiRenderer`` so both are testable in isolation. Only the
main loop / stdin / signal handling is here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from tesseract.orchestrator.agent_controller.dispatcher import (
    DispatcherError,
    ensure_daemon_running,
)
from tesseract.orchestrator.agent_controller.ipc_client import (
    ControllerClient,
    ControllerClientError,
)
from tesseract.orchestrator.agent_controller.renderer import TuiRenderer
from tesseract.orchestrator.agent_controller.slash_commands import (
    dispatch as dispatch_slash,
    is_slash_command,
)
from tesseract.orchestrator.agent_controller.trust import (
    is_trusted,
    prompt_trust,
)

log = logging.getLogger(__name__)


_PROMPT = "» "


# ── arg parsing ───────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="the assistant terminal client — attach to the controller daemon",
    )
    parser.add_argument(
        "--session",
        help="attach directly to this controller session id",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="mint a new session and attach",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list active sessions and exit",
    )
    parser.add_argument(
        "--title", help="optional title for a new session"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color output",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help=(
            "leave the controller daemon running on exit instead of "
            "shutting it down. Use when you want detach-and-reattach "
            "later (the original headless-survive pattern)."
        ),
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help=(
            "shut down any running controller daemon before attaching "
            "so the new daemon picks up code edits. Pairs naturally "
            "with iterative development."
        ),
    )
    parser.add_argument(
        "--shutdown",
        action="store_true",
        help=(
            "shut the controller daemon down and exit. Does not attach "
            "to a session. No-op if no daemon is running."
        ),
    )
    parser.add_argument(
        "--delete",
        metavar="SESSION_ID",
        help=(
            "delete a session's record + transcript and exit. Refuses "
            "if the session is currently attached — detach first."
        ),
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help=(
            "use the legacy ANSI renderer instead of the Textual app. "
            "Auto-selected when stdout is not a TTY (CI, piped output)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("TESSERACT_LOG_LEVEL", "WARNING"),
    )
    return parser


# ── interactive picker ───────────────────────────────────────────────


async def _pick_or_create_session(
    client: ControllerClient,
    *,
    title: str | None,
) -> dict[str, Any]:
    sessions = await client.list_sessions()
    if not sessions:
        print("no active sessions — creating a new one.")
        return await client.new_session(title=title, origin="cli")

    print("active sessions:")
    for i, sess in enumerate(sessions, start=1):
        sid = sess.get("session_id", "?")
        sess_title = sess.get("title") or "(untitled)"
        status = sess.get("status") or "?"
        print(f"  [{i}] {sid}  {status}  {sess_title}")
    print(f"  [n] new session")
    while True:
        try:
            choice = (await _ainput(f"pick {_PROMPT}")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise
        if choice in ("n", "new"):
            return await client.new_session(title=title, origin="cli")
        try:
            idx = int(choice) - 1
        except ValueError:
            print("enter a number or 'n' for new")
            continue
        if 0 <= idx < len(sessions):
            return await client.attach(sessions[idx]["session_id"])
        print("out of range — try again")


# ── stdin async wrapper ──────────────────────────────────────────────


# Single ``PromptSession`` reused for the lifetime of the CLI so the
# input box keeps a stable history + key bindings + style. ``patch_stdout``
# wraps push-loop writes so they don't shred the input line while the
# operator is typing — the Claude-CLI affordance we were missing.
_PROMPT_SESSION: Any | None = None
_PROMPT_AVAILABLE = True


def _get_prompt_session() -> Any | None:
    global _PROMPT_SESSION, _PROMPT_AVAILABLE
    if not _PROMPT_AVAILABLE:
        return None
    if _PROMPT_SESSION is not None:
        return _PROMPT_SESSION
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.styles import Style

        style = Style.from_dict({
            "": "ansibrightmagenta",
            "prompt": "ansibrightmagenta bold",
            "frame": "ansibrightblack",
        })
        _PROMPT_SESSION = PromptSession(style=style, multiline=False)
        return _PROMPT_SESSION
    except Exception:  # noqa: BLE001 — fall back to plain stdin
        log.debug("agent: prompt_toolkit unavailable; falling back to stdin",
                  exc_info=True)
        _PROMPT_AVAILABLE = False
        return None


async def _ainput(prompt: str = "") -> str:
    """Read a line of input. Uses ``prompt_toolkit`` when available so
    the input renders as a real bordered prompt (with cursor + history)
    instead of bare stdin echo. Falls back to ``input()`` if
    ``prompt_toolkit`` isn't importable (CI, non-TTY).
    """
    session = _get_prompt_session()
    if session is None:
        return await asyncio.to_thread(input, prompt)
    from prompt_toolkit.formatted_text import FormattedText

    # Render the prompt as `> ` in bright magenta so it stands out from
    # the (greyer) assistant output above.
    fragments = FormattedText([("class:prompt", prompt or "> ")])
    try:
        return await session.prompt_async(fragments)
    except (EOFError, KeyboardInterrupt):
        raise


def _patch_stdout_context() -> Any:
    """Return a context manager that re-renders the prompt line after
    every push-loop write, so streaming assistant text and tool blocks
    don't shred the operator's input. Returns a no-op context when
    ``prompt_toolkit`` isn't available so callers don't have to branch.
    """
    if not _PROMPT_AVAILABLE:
        from contextlib import nullcontext

        return nullcontext()
    try:
        from prompt_toolkit.patch_stdout import patch_stdout

        return patch_stdout(raw=True)
    except Exception:  # noqa: BLE001
        from contextlib import nullcontext

        return nullcontext()


# ── main loop ────────────────────────────────────────────────────────


class _TuiSession:
    """Per-session runtime — owns the renderer, the active session id,
    and the Ctrl-C tally."""

    def __init__(
        self,
        client: ControllerClient,
        renderer: TuiRenderer,
        session_id: str,
    ) -> None:
        self.client = client
        self.renderer = renderer
        self.session_id = session_id
        self.last_worker_id: str | None = None
        self.last_tool_use_id: str | None = None
        self.detach_requested = False
        # Slash commands ``/detach`` / ``/shutdown`` override the CLI's
        # default shutdown-on-exit policy. ``None`` means "use the
        # ``shutdown_on_exit`` flag passed to ``_drive_session``."
        self.keep_daemon_on_exit: bool | None = None

    def remember(self, event: dict[str, Any]) -> None:
        """Cache the most recent worker_id / tool_use_id so Ctrl-C and
        approval prompts know what to act on without round-tripping."""
        kind = event.get("kind")
        if kind == "worker_status":
            wid = event.get("worker_id")
            if isinstance(wid, str):
                self.last_worker_id = wid
        elif kind == "permission_request" and not event.get("resolved"):
            tuid = event.get("tool_use_id")
            if isinstance(tuid, str):
                self.last_tool_use_id = tuid

    async def stdin_loop(self) -> None:
        """Read operator input forever; push as `user_input` IPC."""
        while not self.detach_requested:
            try:
                text = await _ainput(_PROMPT)
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D / Ctrl-C handled at the outer signal layer
                return
            text = text.strip()
            if not text:
                continue
            if text in (":quit", ":q"):
                self.detach_requested = True
                return
            if is_slash_command(text):
                await dispatch_slash(self, text)
                if self.detach_requested:
                    return
                continue
            cmd = text.split(maxsplit=1)[0]
            if cmd == ":approve":
                await self._handle_approval(text, approved=True)
                continue
            if cmd == ":deny":
                await self._handle_approval(text, approved=False)
                continue
            try:
                await self.client.user_input(self.session_id, text)
            except ControllerClientError as exc:
                print(f"send failed: {exc}", file=sys.stderr)
                return

    async def _handle_approval(self, text: str, *, approved: bool) -> None:
        parts = text.split(maxsplit=1)
        tool_use_id = (
            parts[1].strip()
            if len(parts) > 1 and parts[1].strip()
            else self.last_tool_use_id
        )
        if not tool_use_id:
            print(
                "no pending permission to "
                f"{'approve' if approved else 'deny'}",
                file=sys.stderr,
            )
            return
        await self.client.approval(
            self.session_id, tool_use_id, approved=approved
        )

    async def push_loop(self) -> None:
        async for payload in self.client.pushes():
            event_name = payload.get("event")
            if event_name == "_disconnected":
                print(
                    "\n· controller disconnected — exiting",
                    file=sys.stderr,
                )
                self.detach_requested = True
                return
            if event_name == "transcript_event":
                transcript = payload.get("transcript_event") or {}
                self.remember(transcript)
                self.renderer.render(transcript)
                continue
            if event_name == "session_status":
                self.renderer.render_session_status(payload)
                continue
            if event_name == "reload_complete":
                self.renderer.render_reload_complete(payload)
                continue
            if event_name == "session_deleted":
                self.renderer.render_session_deleted(payload)
                # If another client / a future ``/delete`` ever removes
                # the currently-attached session out from under us, exit
                # cleanly rather than waiting for IPC errors on the next
                # turn. v1's ``/delete`` refuses on the local side, but
                # an out-of-band deletion (Mirror UI, second agent window)
                # can still trip this.
                if payload.get("session_id") == self.session_id:
                    self.keep_daemon_on_exit = True
                    self.detach_requested = True
                continue
            if event_name == "session_renamed":
                self.renderer.render_session_renamed(payload)
                continue
            if event_name == "error":
                code = payload.get("code") or "error"
                detail = payload.get("detail") or ""
                print(f"controller error [{code}]: {detail}", file=sys.stderr)
                continue
            # ack / unknown — silently ignored at the operator level
            log.debug("agent: drop push event %s", event_name)


async def _drive_session(
    client: ControllerClient,
    renderer: TuiRenderer,
    attached: dict[str, Any],
    *,
    shutdown_on_exit: bool,
) -> int:
    record = attached.get("session") or {}
    session_id = record.get("session_id")
    if not isinstance(session_id, str):
        print("controller returned no session_id; exiting", file=sys.stderr)
        return 1
    renderer.render_header(session_id=session_id)
    # Replay any prior events the daemon sent in `attached`.
    for evt in attached.get("replay_events") or []:
        renderer.render(evt)

    tui = _TuiSession(client, renderer, session_id)
    # Run both loops concurrently — push_loop is the source of truth
    # for "session ended" via `detach_requested`; stdin_loop returns
    # on Ctrl-D / :quit and flips the same flag. ``patch_stdout``
    # wraps the push-loop's writes so they don't shred the operator's
    # input line while typing — the Claude-CLI affordance.
    stdin_task = asyncio.create_task(tui.stdin_loop(), name="agent-stdin")
    push_task = asyncio.create_task(tui.push_loop(), name="agent-push")
    try:
        with _patch_stdout_context():
            await asyncio.wait(
                {stdin_task, push_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
    finally:
        tui.detach_requested = True
        for task in (stdin_task, push_task):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        # 2026-05-24 — default exit semantics tear the daemon down.
        # `--keep` overrides this with the old detach-only behaviour.
        # Slash commands ``/detach`` and ``/shutdown`` further override
        # via ``keep_daemon_on_exit`` (True/False); ``None`` means
        # "use the CLI's default ``shutdown_on_exit``."
        if tui.keep_daemon_on_exit is None:
            do_shutdown = shutdown_on_exit
        else:
            do_shutdown = not tui.keep_daemon_on_exit
        # Order: shutdown FIRST (it acks + sets the daemon's exit
        # event), then detach is harmless since the connection is
        # about to close. If shutdown fails (daemon already gone, etc.),
        # fall back to a plain detach.
        if do_shutdown:
            try:
                await client.shutdown()
            except ControllerClientError:
                pass
        else:
            try:
                await client.detach(session_id)
            except ControllerClientError:
                pass
    return 0


# ── entry ─────────────────────────────────────────────────────────────


async def _async_main(args: argparse.Namespace) -> int:
    # `--shutdown` is a pure teardown — skip trust + session pickers.
    if args.shutdown:
        return await _shutdown_running_daemon()

    # `--restart` kills any existing daemon first so the next attach
    # spawns a fresh one with the latest code.
    if args.restart:
        await _shutdown_running_daemon()

    # Trust gate — the claude/codex CLIs ask once per workspace; we
    # mirror that. Skipped on read-only / daemon-administrative paths
    # (``--list``, ``--delete``) since they don't expose workspace
    # state to the chat brain.
    skip_trust = bool(args.list or args.delete)
    if not skip_trust and not is_trusted(os.getcwd()):
        approved = await asyncio.to_thread(prompt_trust, os.getcwd())
        if not approved:
            print("agent: directory not trusted; aborting", file=sys.stderr)
            return 3

    # Self-bootstrap — if no daemon is running, spawn one and wait for
    # it to come up. Mirrors the claude/codex UX where the bare command
    # just works whether or not the supervisor is up. ``--list`` /
    # ``--delete`` skip the spawn because their natural answer when
    # nothing's running is "no sessions" / "nothing to delete" rather
    # than a 25s cold start.
    spawn = not (args.list or args.delete)
    try:
        await ensure_daemon_running(spawn_if_missing=spawn)
    except DispatcherError as exc:
        print(f"agent: could not start controller daemon: {exc}", file=sys.stderr)
        return 2

    try:
        client = await ControllerClient.connect()
    except ControllerClientError as exc:
        # ``--delete`` falls back to direct registry deletion when no
        # daemon is running. Sessions live on disk; with no daemon up
        # nothing is attached so the attached-session race is moot, and
        # forcing a 25s cold start just to delete one file would be
        # hostile. Surfaces the same exit-0 message as the IPC path.
        if args.delete:
            return _delete_session_offline(args.delete)
        print(f"agent: {exc}", file=sys.stderr)
        return 2

    async with client:
        if args.list:
            sessions = await client.list_sessions()
            print(json.dumps(sessions, indent=2))
            return 0
        if args.delete:
            try:
                push = await client.delete_session(args.delete)
            except ControllerClientError as exc:
                print(f"agent: delete failed: {exc}", file=sys.stderr)
                return 2
            sid = push.get("session_id") or args.delete
            print(f"deleted session {sid}")
            return 0
        if args.new:
            attached = await client.new_session(
                title=args.title, origin="cli"
            )
        elif args.session:
            attached = await client.attach(args.session)
        else:
            try:
                attached = await _pick_or_create_session(
                    client, title=args.title
                )
            except (EOFError, KeyboardInterrupt):
                return 130
        # Default: launch the Textual app (scrollable transcript,
        # collapsible tool blocks, status bar, real input field).
        # Falls back to the legacy ANSI renderer when stdout isn't a
        # TTY (CI / piped output) or when ``--legacy`` is set.
        use_textual = (
            not args.legacy
            and sys.stdout.isatty()
            and sys.stdin.isatty()
        )
        if use_textual:
            return await _drive_session_textual(
                client,
                attached,
                shutdown_on_exit=not args.keep,
            )
        renderer = TuiRenderer(color=not args.no_color)
        return await _drive_session(
            client,
            renderer,
            attached,
            shutdown_on_exit=not args.keep,
        )


async def _drive_session_textual(
    client: ControllerClient,
    attached: dict[str, Any],
    *,
    shutdown_on_exit: bool,
) -> int:
    """Run the Textual app for one session attach.

    The app owns its own push loop / input handling / lifecycle so this
    helper is thin: extract the session id from the attach payload,
    construct ``AgentApp`` with the live client, and run until the app
    exits (Ctrl+C / ``:quit`` / disconnect).
    """
    from tesseract.scripts.agent_app import AgentApp

    record = attached.get("session") or {}
    session_id = record.get("session_id")
    if not isinstance(session_id, str):
        print("controller returned no session_id; exiting", file=sys.stderr)
        return 1
    app = AgentApp(
        client=client,
        session_id=session_id,
        shutdown_on_exit=shutdown_on_exit,
        replay_events=attached.get("replay_events") or [],
    )
    return await app.run_async() or 0


def _delete_session_offline(session_id: str) -> int:
    """Delete a session record + transcript directly via the registry.

    Used by ``agent --delete`` when no daemon is running — sessions are
    canonical on disk and no client can be attached without a daemon,
    so the attached-session race the IPC handler guards against is
    moot. Errors (bad id format, OS error) print and return non-zero.
    """
    from tesseract.orchestrator.agent_controller.sessions import (
        SessionRegistry,
    )

    try:
        existed = SessionRegistry().delete_session(session_id)
    except ValueError as exc:
        # Bad session-id shape — bubbles up from
        # ``paths._validate_session_id``.
        print(f"agent: delete failed: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"agent: delete failed: {exc}", file=sys.stderr)
        return 2
    if existed:
        print(f"deleted session {session_id}")
    else:
        print(f"no such session: {session_id}", file=sys.stderr)
    return 0 if existed else 1


async def _shutdown_running_daemon() -> int:
    """Best-effort: connect, send shutdown, return. No-op if no daemon
    is running. Used by ``--shutdown`` and ``--restart``."""
    try:
        client = await ControllerClient.connect()
    except ControllerClientError:
        # No daemon to shut down — that's success for the user's intent.
        return 0
    try:
        async with client:
            await client.shutdown()
    except ControllerClientError as exc:
        print(f"agent: shutdown send failed: {exc}", file=sys.stderr)
        return 2
    # Brief wait for the daemon's port-file unlink so a subsequent
    # `ensure_daemon_running` doesn't observe the stale port.
    from tesseract.orchestrator.agent_controller.paths import port_file_path

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if not port_file_path().exists():
            break
        await asyncio.sleep(0.1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
