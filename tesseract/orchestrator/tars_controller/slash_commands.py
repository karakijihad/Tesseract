"""In-session ``/command`` parser + dispatch for the ``tars`` TUI.

The stdin loop in :mod:`tesseract.scripts.tars_cli` consults
:func:`is_slash_command` to decide whether to treat a line of input as
a slash command or to forward it to the daemon as ``user_input``. On a
match it calls :func:`dispatch` which runs the command against the
attached :class:`_TuiSession`.

Commands fall into three rough buckets:

* **Local-only** — ``/help``, ``/clear``: render output, no IPC.
* **IPC request/response** — ``/sessions``, ``/new``, ``/delete``,
  ``/title``, ``/reload``: send a message, await the structured
  response, render it. Errors surface as :class:`ControllerClientError`
  and are caught + printed by the dispatcher (the stdin loop should
  not crash on a typo'd command).
* **Session exit** — ``/quit``, ``/detach``, ``/shutdown``: flip flags
  on the :class:`_TuiSession` so the main loop tears the attach down
  with the requested shutdown semantics.

The dispatcher is intentionally small: every command is a coroutine
that takes the :class:`_TuiSession` and the post-prefix argument
string. Adding a command means appending one row to ``_COMMANDS``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from .ipc_client import ControllerClientError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from tesseract.scripts.tars_cli import _TuiSession

log = logging.getLogger(__name__)


SLASH_PREFIX = "/"


def is_slash_command(text: str) -> bool:
    """True only when the first whitespace-bounded token after ``/``
    is all ASCII letters. Lets paths like ``/etc/hosts`` and
    ``/dev/null`` fall through to ``user_input`` since their first
    token (``etc/hosts``, ``dev/null``) contains a ``/``. Every name
    in :data:`_COMMANDS` is pure-alpha so this never blocks a real
    command."""
    if not text or not text.startswith(SLASH_PREFIX):
        return False
    rest = text[len(SLASH_PREFIX):]
    if not rest:
        return False
    head = rest.split(maxsplit=1)[0]
    return head.isalpha()


SlashHandler = Callable[["_TuiSession", str], Awaitable[None]]


async def _cmd_help(tui: "_TuiSession", _args: str) -> None:
    entries = [(f"/{name}", desc) for name, _, desc in _COMMANDS]
    tui.renderer.render_help(entries)


async def _cmd_clear(tui: "_TuiSession", _args: str) -> None:
    tui.renderer.render_clear()


async def _cmd_sessions(tui: "_TuiSession", _args: str) -> None:
    try:
        sessions = await tui.client.list_sessions()
    except ControllerClientError as exc:
        tui.renderer.render_notice(f"sessions: {exc}", style="red")
        return
    if not sessions:
        tui.renderer.render_notice("no sessions", style="grey50")
        return
    tui.renderer.render_notice(
        f"{len(sessions)} session(s):", style="bold cyan"
    )
    for sess in sessions:
        sid = sess.get("session_id") or "?"
        status = sess.get("status") or "?"
        title = sess.get("title") or "(untitled)"
        marker = "● " if sid == tui.session_id else "  "
        tui.renderer.render_notice(
            f"{marker}{sid}  {status}  {title}", style="grey70"
        )


async def _cmd_new(tui: "_TuiSession", args: str) -> None:
    title = args.strip() or None
    try:
        attached = await tui.client.new_session(title=title, origin="cli")
    except ControllerClientError as exc:
        tui.renderer.render_notice(f"new: {exc}", style="red")
        return
    record = attached.get("session") or {}
    new_id = record.get("session_id") or "?"
    tui.renderer.render_notice(
        f"+ minted session {new_id}"
        f" — run `tars --session {new_id}` to attach",
        style="green",
    )


async def _cmd_delete(tui: "_TuiSession", args: str) -> None:
    target = args.strip()
    if not target:
        tui.renderer.render_notice(
            "/delete requires a session id (see /sessions)", style="red"
        )
        return
    if target == tui.session_id:
        tui.renderer.render_notice(
            "/delete cannot remove the attached session — "
            "run /quit, then `tars --delete <id>`",
            style="red",
        )
        return
    try:
        push = await tui.client.delete_session(target)
    except ControllerClientError as exc:
        tui.renderer.render_notice(f"delete: {exc}", style="red")
        return
    tui.renderer.render_session_deleted(push)


async def _cmd_title(tui: "_TuiSession", args: str) -> None:
    new_title = args.strip()
    if not new_title:
        tui.renderer.render_notice(
            "/title requires a new title", style="red"
        )
        return
    try:
        push = await tui.client.rename_session(tui.session_id, new_title)
    except ControllerClientError as exc:
        tui.renderer.render_notice(f"title: {exc}", style="red")
        return
    tui.renderer.render_session_renamed(push)


async def _cmd_reload(tui: "_TuiSession", args: str) -> None:
    target = args.strip() or "all"
    if target not in {"config", "roles", "tools", "all"}:
        tui.renderer.render_notice(
            f"/reload: unknown target {target!r} "
            "(config|roles|tools|all)",
            style="red",
        )
        return
    try:
        push = await tui.client.reload(target)
    except ControllerClientError as exc:
        tui.renderer.render_notice(f"reload: {exc}", style="red")
        return
    tui.renderer.render_reload_complete(push)


async def _cmd_detach(tui: "_TuiSession", _args: str) -> None:
    # ``/detach`` keeps the daemon alive regardless of the CLI's
    # default shutdown-on-exit policy. The main loop reads
    # ``keep_daemon_on_exit`` to decide whether to send ``shutdown``
    # or just ``detach`` during teardown.
    tui.keep_daemon_on_exit = True
    tui.detach_requested = True


async def _cmd_quit(tui: "_TuiSession", _args: str) -> None:
    # Defer to the CLI's existing ``shutdown_on_exit`` flag (set by
    # ``--keep``) so ``/quit`` keeps the same UX as Ctrl-C / EOF.
    tui.detach_requested = True


async def _cmd_shutdown(tui: "_TuiSession", _args: str) -> None:
    tui.keep_daemon_on_exit = False
    tui.detach_requested = True


# Order matters — used to render ``/help``. Description column wraps to
# one line in the renderer.
_COMMANDS: list[tuple[str, SlashHandler, str]] = [
    ("help", _cmd_help, "Show this help"),
    ("clear", _cmd_clear, "Clear the screen"),
    ("sessions", _cmd_sessions, "List sessions (● marks current)"),
    ("new", _cmd_new, "Mint a new session ([title] optional)"),
    ("delete", _cmd_delete, "Delete <id> — not the attached session"),
    ("title", _cmd_title, "Rename the current session: /title <text>"),
    ("reload", _cmd_reload, "Reload runtime config [config|roles|tools|all]"),
    ("detach", _cmd_detach, "Exit and leave the daemon running"),
    ("quit", _cmd_quit, "Exit (same teardown as Ctrl-C / :quit)"),
    ("shutdown", _cmd_shutdown, "Exit and shut the daemon down"),
]

_COMMAND_MAP: dict[str, SlashHandler] = {
    name: handler for name, handler, _ in _COMMANDS
}


async def dispatch(tui: "_TuiSession", text: str) -> None:
    """Dispatch a single slash-command line on the attached
    :class:`_TuiSession`. Unknown commands render a hint pointing at
    ``/help`` so a typo doesn't fall through to ``user_input``."""
    stripped = text.strip()
    if not is_slash_command(stripped):  # pragma: no cover — caller checks
        return
    # Trim the leading ``/`` then split first token from the rest.
    payload = stripped[len(SLASH_PREFIX):]
    parts = payload.split(maxsplit=1)
    name = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    handler = _COMMAND_MAP.get(name)
    if handler is None:
        tui.renderer.render_notice(
            f"unknown command: /{name} (try /help)", style="red"
        )
        return
    try:
        await handler(tui, args)
    except Exception as exc:  # noqa: BLE001 — never crash the TUI loop
        log.exception("slash command /%s failed: %s", name, exc)
        tui.renderer.render_notice(
            f"/{name}: {exc}", style="red"
        )


def known_commands() -> list[str]:
    """Test hook — names without the ``/`` prefix."""
    return [name for name, _, _ in _COMMANDS]


__all__ = [
    "SLASH_PREFIX",
    "dispatch",
    "is_slash_command",
    "known_commands",
]
