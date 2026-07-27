"""Unified slash-command registry — Mirror session ops + ToolRegistry.

Mirror chat slash dispatch (`ws.py::_handle_command`) consults this registry
to look up a `CommandSpec` for the typed `/<head>`. Two command families
coexist behind one surface:

- **Mirror session commands** (`source="mirror_session"`): the ~11 hand-wired
  `cmd_*` functions in `commands.py`. They mutate per-WS session state
  (`reset`, `save`, `load`, `compact`), broadcast to other sessions
  (`mode`), or emit Mirror-specific envelopes that don't fit the generic
  ToolResult shape (`alarm-*`, `schedule-*`).

- **Kernel tools** (`source="kernel_tool"`): every `Tool` in `ToolRegistry`
  not already shadowed by a Mirror session command. Exposed via a thin
  adapter that calls `slash_dispatch.run_slash` with
  `_MIRROR_OPERATOR_TOKEN`. Mirror chat is operator-only — same trust model
  as the REPL, same security gates (DENY + write-path validation still
  fire; policy posture intentionally bypassed because the operator
  initiated the call).

Aliases let the existing kebab-case forms (`/alarm-set`) coexist with the
canonical snake_case kernel form (`/alarm_set`). Lookup tries the head as
typed, then falls back to its snake_case form.

The frontend autocomplete is hydrated from `GET /api/commands` which
serializes this registry — never go back to a hardcoded list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

from aiohttp import web
from pydantic import BaseModel

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import Tool
from tesseract.mirror.server import commands as cmd_mod
from tesseract.mirror.server.envelope import make_envelope
from tesseract.mirror.server.session import ServerSession, send_envelope
from tesseract.scripts.slash_dispatch import (
    _MIRROR_OPERATOR_TOKEN,
    _usage_for,
    parse_slash,
    run_slash,
)

CommandSource = Literal["mirror_session", "kernel_tool"]
CommandHandler = Callable[[web.Application, ServerSession, str | None], Awaitable[None]]


@dataclass(frozen=True)
class CommandSpec:
    """One row in the unified slash-command registry.

    `name` is the canonical (snake_case) form. `aliases` lists alternate
    forms accepted by the dispatcher (e.g. kebab-case for back-compat).
    `arg_label` is a one-line `<required> [optional]` shape; `arg_help`
    is a free-form sentence shown beneath the hint summary.
    """

    name: str
    summary: str
    handler: CommandHandler
    source: CommandSource = "mirror_session"
    aliases: tuple[str, ...] = ()
    arg_label: str | None = None
    arg_help: str | None = None
    mutates_session: bool = False  # blocks during a busy turn
    emit_stats_after: bool = False  # call emit_stats after handler returns


def _normalize(head: str) -> str:
    """Strip leading slash, lower-case, hyphens to underscores."""
    if head.startswith("/"):
        head = head[1:]
    return head.lower().replace("-", "_")


# ── Mirror session command handler adapters ──────────────────────────────────
# Each adapter normalizes (app, session, raw_arg_remainder) → calls the
# matching `cmd_*` from `commands.py`. raw_arg_remainder is the free text
# after the head (or None if absent).


def _split_first(arg: str | None) -> str | None:
    """For commands that only want the first whitespace-separated token."""
    if not arg:
        return None
    return arg.split(None, 1)[0] if arg else None


async def _h_sessions(app, session, _arg):
    await cmd_mod.cmd_sessions(session)


async def _h_save(app, session, arg):
    await cmd_mod.cmd_save(app, session, arg)


async def _h_load(app, session, arg):
    await cmd_mod.cmd_load(app, session, _split_first(arg))


async def _h_reset(app, session, arg):
    await cmd_mod.cmd_reset(app, session, _split_first(arg))


async def _h_compact(app, session, _arg):
    await cmd_mod.cmd_compact(app, session)


async def _h_compact_file(app, session, arg):
    await cmd_mod.cmd_compact_file(app, session, _split_first(arg))


async def _h_delete(app, session, arg):
    await cmd_mod.cmd_delete(session, _split_first(arg))


async def _h_reflect(app, session, _arg):
    await cmd_mod.cmd_reflect(app, session)


async def _h_soul_show(app, session, _arg):
    await cmd_mod.cmd_soul_show(session)


async def _h_observe(app, session, arg):
    await cmd_mod.cmd_observe(app, session, _split_first(arg))


async def _h_mode(app, session, arg):
    await cmd_mod.cmd_mode(app, session, _split_first(arg))


# ── Alarms: arg is the FULL remainder, not split, so multi-word phrases
#    like '"every weekday at 9am" stand up' parse correctly.


async def _h_alarm_set(app, session, arg):
    await cmd_mod.cmd_alarm_set(app, session, arg or None)


async def _h_alarm_cancel(app, session, arg):
    await cmd_mod.cmd_alarm_cancel(app, session, _split_first(arg))


async def _h_alarm_list(app, session, _arg):
    await cmd_mod.cmd_alarm_list(app, session)


async def _h_alarm_snooze(app, session, arg):
    await cmd_mod.cmd_alarm_snooze(app, session, arg or None)


async def _h_alarm_dismiss(app, session, arg):
    await cmd_mod.cmd_alarm_dismiss(app, session, _split_first(arg))


# ── Schedule


async def _h_schedule_enable(app, session, arg):
    await cmd_mod.cmd_schedule_enable(app, session, _split_first(arg))


async def _h_schedule_disable(app, session, arg):
    await cmd_mod.cmd_schedule_disable(app, session, _split_first(arg))


async def _h_schedule_set_cadence(app, session, arg):
    await cmd_mod.cmd_schedule_set_cadence(app, session, arg or None)


async def _h_schedule_run_now(app, session, arg):
    await cmd_mod.cmd_schedule_run_now(app, session, _split_first(arg))


async def _h_schedule_set_role(app, session, arg):
    await cmd_mod.cmd_schedule_set_role(app, session, arg or None)


# ── Stats ── handled inline in dispatcher, no cmd_* needed
async def _h_stats(app, session, _arg):
    # Late import to avoid circular: ws.py imports this module, and ws.py
    # owns emit_stats. Importing at module load would cycle.
    from tesseract.mirror.server.ws import emit_stats
    await emit_stats(app, session)


# ── Static spec list for Mirror session commands ─────────────────────────────


_MIRROR_SESSION_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="sessions",
        summary="list saved sessions",
        handler=_h_sessions,
    ),
    CommandSpec(
        name="save",
        summary="save current session",
        handler=_h_save,
        arg_label="[name]",
        arg_help="defaults to the current save name or a fresh timestamp",
    ),
    CommandSpec(
        name="load",
        summary="replace history with a saved session",
        handler=_h_load,
        aliases=("resume",),
        arg_label="<name>",
        arg_help="name of the saved session file (without .json)",
        mutates_session=True,
        emit_stats_after=True,
    ),
    CommandSpec(
        name="reset",
        summary="clear the conversation; the dialog asks whether to reflect first",
        handler=_h_reset,
        arg_label="[reflect|clear]",
        arg_help="bare /reset opens the confirm dialog; reflect = autosave+reflect+clear, clear = wipe with zero side effects",
        mutates_session=True,
        emit_stats_after=True,
    ),
    CommandSpec(
        name="compact",
        summary="summarize and trim history",
        handler=_h_compact,
        mutates_session=True,
        emit_stats_after=True,
    ),
    CommandSpec(
        name="compact_file",
        summary="compact a saved session file without disturbing the live session",
        handler=_h_compact_file,
        arg_label="<name>",
        arg_help="name of the saved session file to compact in place",
        mutates_session=True,
    ),
    CommandSpec(
        name="stats",
        summary="show tokens / turns / threshold",
        handler=_h_stats,
    ),
    CommandSpec(
        name="delete",
        summary="delete a saved session file",
        handler=_h_delete,
        arg_label="<name>",
    ),
    CommandSpec(
        name="reflect",
        summary="reflect on session + run librarian pass",
        handler=_h_reflect,
    ),
    CommandSpec(
        name="soul_show",
        summary="display SOUL.md contents",
        handler=_h_soul_show,
        aliases=("soul-show",),
    ),
    CommandSpec(
        name="observe",
        summary="one-shot observer pass",
        handler=_h_observe,
        arg_label="[meta|maintenance]",
        arg_help="defaults to 'meta'",
    ),
    CommandSpec(
        name="mode",
        summary="change security mode",
        handler=_h_mode,
        arg_label="<max|standard|headless>",
    ),
    CommandSpec(
        name="alarm_set",
        summary="queue an alarm (one-shot or recurring)",
        handler=_h_alarm_set,
        aliases=("alarm-set",),
        arg_label="<label> <when> [message]",
        arg_help="when: '20m', '9am', 'tomorrow at 9am', 'every weekday at 9am'",
    ),
    CommandSpec(
        name="alarm_cancel",
        summary="cancel a pending alarm",
        handler=_h_alarm_cancel,
        aliases=("alarm-cancel", "alarm-delete", "alarm_delete"),
        arg_label="<handle>",
        arg_help="alarm label or id-prefix",
    ),
    CommandSpec(
        name="alarm_list",
        summary="list pending alarms",
        handler=_h_alarm_list,
        aliases=("alarm-list",),
    ),
    CommandSpec(
        name="alarm_snooze",
        summary="snooze a pending or just-fired alarm",
        handler=_h_alarm_snooze,
        aliases=("alarm-snooze",),
        arg_label="<handle> [duration]",
        arg_help="duration defaults to 10m",
    ),
    CommandSpec(
        name="alarm_dismiss",
        summary="dismiss a just-fired alarm",
        handler=_h_alarm_dismiss,
        aliases=("alarm-dismiss",),
        arg_label="<handle>",
    ),
    CommandSpec(
        name="schedule_enable",
        summary="enable a scheduler job",
        handler=_h_schedule_enable,
        aliases=("schedule-enable",),
        arg_label="<name>",
    ),
    CommandSpec(
        name="schedule_disable",
        summary="disable a scheduler job",
        handler=_h_schedule_disable,
        aliases=("schedule-disable",),
        arg_label="<name>",
    ),
    CommandSpec(
        name="schedule_set_cadence",
        summary="update a job cadence at runtime",
        handler=_h_schedule_set_cadence,
        aliases=("schedule-set-cadence",),
        arg_label="<name> <cron-or-interval>",
    ),
    CommandSpec(
        name="schedule_run_now",
        summary="fire a scheduler job off-schedule",
        handler=_h_schedule_run_now,
        aliases=("schedule-run-now",),
        arg_label="<name>",
    ),
    CommandSpec(
        name="schedule_set_role",
        summary="set the LLM role a scheduler job uses (or '-' to clear)",
        handler=_h_schedule_set_role,
        aliases=("schedule-set-role",),
        arg_label="<name> <role-or-dash>",
        arg_help="role names come from roles.yaml; pass '-' to revert to the handler default",
    ),
)


# ── Kernel-tool adapter ──────────────────────────────────────────────────────


def _arg_label_for_tool(tool: Tool) -> str | None:
    """`<req> [opt]` shape for the hint panel. None if the schema is empty."""
    parts: list[str] = []
    for n, info in tool.input_schema.model_fields.items():
        parts.append(f"<{n}>" if info.is_required() else f"[{n}]")
    return " ".join(parts) if parts else None


def _arg_help_for_tool(tool: Tool) -> str | None:
    """Compact `field: description` chunks from the Pydantic schema, joined
    with ` · ` so the run-on string still reads cleanly inside the hint
    `<span>` (HTML collapses newlines in span text)."""
    chunks: list[str] = []
    for n, info in tool.input_schema.model_fields.items():
        desc = (info.description or "").strip()
        if not desc:
            continue
        chunks.append(f"{n}: {desc}")
    return " · ".join(chunks) if chunks else None


def _make_kernel_tool_handler(tool_name: str) -> CommandHandler:
    async def handler(app: web.Application, session: ServerSession, arg: str | None) -> None:
        registry: ToolRegistry = app["tool_registry"]
        line = f"/{tool_name}" + (f" {arg.strip()}" if arg else "")
        try:
            parsed = parse_slash(line)
        except ValueError as exc:
            await _emit_kernel_result(session, tool_name, f"[invalid args: {exc}]", is_error=True)
            return
        if parsed is None:
            await _emit_kernel_result(session, tool_name, "[empty command]", is_error=True)
            return
        name, kv, positional = parsed
        tool_context = session.chat_session.tool_context
        try:
            output = await run_slash(
                registry, name, kv, positional, tool_context,
                caller_token=_MIRROR_OPERATOR_TOKEN,
            )
        except Exception as exc:
            await _emit_kernel_result(session, tool_name, f"[tool error] {type(exc).__name__}: {exc}", is_error=True)
            return
        is_error = output.startswith("[denied") or output.startswith("[error") or output.startswith("[tool error")
        await _emit_kernel_result(session, tool_name, output, is_error=is_error)

    return handler


async def _emit_kernel_result(
    session: ServerSession, tool_name: str, output: str, *, is_error: bool
) -> None:
    await send_envelope(session, make_envelope(
        "tool_slash_result", "command", session.session_id,
        {"name": tool_name, "output": output, "is_error": is_error},
    ))


# ── Registry assembly ────────────────────────────────────────────────────────


@dataclass
class CommandRegistry:
    by_name: dict[str, CommandSpec] = field(default_factory=dict)

    def lookup(self, head: str) -> CommandSpec | None:
        """Try head as typed, then snake-case fallback."""
        spec = self.by_name.get(head.lstrip("/").lower())
        if spec is not None:
            return spec
        return self.by_name.get(_normalize(head))

    def specs(self) -> list[CommandSpec]:
        # de-dup since aliases share the same spec
        seen: set[int] = set()
        out: list[CommandSpec] = []
        for spec in self.by_name.values():
            if id(spec) in seen:
                continue
            seen.add(id(spec))
            out.append(spec)
        return out


def build_command_registry(tool_registry: ToolRegistry) -> CommandRegistry:
    """Assemble the unified slash-command registry.

    Mirror session commands win on name collisions — they have richer UX
    (custom envelopes, session-state mutation) than the generic kernel-tool
    adapter could express. Kernel tools fill in everything else.
    """
    reg = CommandRegistry()

    mirror_taken: set[str] = set()
    for spec in _MIRROR_SESSION_SPECS:
        reg.by_name[spec.name] = spec
        mirror_taken.add(spec.name)
        for alias in spec.aliases:
            reg.by_name[alias.lower()] = spec
            mirror_taken.add(_normalize(alias))

    for tool_name in tool_registry.names():
        canonical = tool_name.lower()
        if canonical in mirror_taken:
            continue  # Mirror session command shadows this kernel tool
        tool = tool_registry.get(tool_name)
        if tool is None:
            continue
        spec = CommandSpec(
            name=tool_name,
            summary=(tool.description or "").splitlines()[0][:160] if tool.description else "",
            handler=_make_kernel_tool_handler(tool_name),
            source="kernel_tool",
            arg_label=_arg_label_for_tool(tool),
            arg_help=_arg_help_for_tool(tool),
        )
        reg.by_name[canonical] = spec

    return reg


def serialize_specs(specs: list[CommandSpec]) -> list[dict]:
    """Shape for `GET /api/commands`. Stable field set — frontend reads this."""
    out: list[dict] = []
    for s in specs:
        out.append({
            "name": s.name,
            "summary": s.summary,
            "source": s.source,
            "aliases": list(s.aliases),
            "arg_label": s.arg_label,
            "arg_help": s.arg_help,
            "mutates_session": s.mutates_session,
        })
    return out
