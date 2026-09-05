"""cockpit_show — the assistant changes what is on the operator's screen.

Answering "where do I change the voice" with a path the operator then has to
walk is the gap this closes: the assistant opens the pane instead, and its
answer becomes something you can see rather than something you have to trust.

Same shape as `orb_visibility`: push an entity envelope to every open Mirror
WS session, and let the frontend write THE SAME STORE the operator's own
control writes (`stores/ui.ts` for the tab, `stores/appearance.ts` for the
size). Neither control can drift from the other, because there is only one
piece of state under both.

Only what the vocabulary below names is reachable. That is the deliberate
half: the alternative, synthetic clicks against CSS selectors, needs no
vocabulary and is a general remote-control primitive over the window the
operator is sitting in.

`default_posture="auto"` — a UI signal with no I/O, visible the instant it
happens and undone by clicking back. Matches `orb_visibility` and `set_state`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)

#: The panels, mirroring `View` in `mirror/src/stores/ui.ts`. Listed rather
#: than free text so a typo is a validation error the model can read, not a
#: command that broadcasts and does nothing.
#:
#: `schedule` and `agents` were here after their panels were deleted, and the
#: cost of that is why the drift test beside this file exists: the frontend
#: casts the name it is sent straight into `openPanel`, which guards only
#: against `orb`, so a stale name reached `VIEW_REGISTRY[kind]()` and threw
#: during render with nothing to catch it.
VIEWS = (
    "orb", "chat", "terminal", "pulse", "workspace", "conscience",
    "identity", "channels", "autonomy", "graph", "settings",
)


class CockpitShowInput(BaseModel):
    action: Literal["show", "zoom", "scroll"] = Field(
        description=(
            "show opens a panel. zoom changes the text size of the whole app. "
            "scroll moves the open panel."
        ),
    )
    view: str | None = Field(
        default=None,
        description=f"Which panel to open. One of: {', '.join(VIEWS)}. Only for show.",
    )
    section: str | None = Field(
        default=None,
        description=(
            "A section inside that panel, by the name on its row in the left "
            "list (\"about\", \"voice\", \"keys\"). Omit to leave the panel "
            "wherever it was."
        ),
    )
    direction: str | None = Field(
        default=None,
        description=(
            "For zoom: in, out or reset. For scroll: up, down, top or bottom."
        ),
    )
    scale: float | None = Field(
        default=None,
        ge=0.5,
        le=1.3,
        description=(
            "An exact text size for zoom, where 1 is the shipped size. Use "
            "direction instead unless the operator named a number."
        ),
    )


class CockpitShowTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "showing-the-operator"
    summary: ClassVar[str] = "Open a panel, change the text size, or scroll, in the operator's window."
    use_when: ClassVar[str] = (
        "The operator asks to be shown something in the app, or asks where a "
        "setting lives: open it rather than describing the path. Also when they "
        "ask you to make the text bigger or smaller, which is the whole app at "
        "once and works from across the room."
    )
    not_when: ClassVar[str] = (
        "`surface_focus` raises one card on the canvas; this moves the window "
        "around it. `open` puts a NEW thing on screen, where this only navigates "
        "what is already built. `browser_scroll` scrolls a web page the assistant "
        "opened, not the operator's own app."
    )
    depends_on: ClassVar[str] = ""

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        """``app_provider`` resolves the Mirror ``web.Application`` at call
        time, the same closure pattern `orb_visibility` and `chat_initiate`
        use, so a REPL or a unit test gets a clean refusal rather than a
        crash."""
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "cockpit_show"

    @property
    def input_schema(self) -> type[BaseModel]:
        return CockpitShowInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    def _validate(self, inp: CockpitShowInput) -> str | None:
        """The consequence, then the remedy. A broadcast that reaches every
        window and moves nothing is the failure mode worth naming precisely."""
        if inp.action == "show":
            if not inp.view:
                return "cockpit_show: `show` needs a view. One of: " + ", ".join(VIEWS)
            if inp.view not in VIEWS:
                return (
                    f"cockpit_show: there is no {inp.view!r} panel, so nothing "
                    f"would open. One of: {', '.join(VIEWS)}"
                )
            return None
        if inp.action == "zoom":
            if inp.scale is None and inp.direction not in ("in", "out", "reset"):
                return (
                    "cockpit_show: `zoom` needs direction in, out or reset, or "
                    "an exact scale between 0.5 and 1.3."
                )
            return None
        if inp.direction not in ("up", "down", "top", "bottom"):
            return "cockpit_show: `scroll` needs direction up, down, top or bottom."
        return None

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, CockpitShowInput)
            else CockpitShowInput(**tool_input.model_dump())
        )

        problem = self._validate(inp)
        if problem is not None:
            return ToolResult(output=problem, is_error=True)

        app = self._app_provider() if self._app_provider is not None else None
        if app is None:
            return ToolResult(
                output=(
                    "cockpit_show: no Mirror app context, so there is no window "
                    "to move. Tell the operator what to open instead."
                ),
                is_error=True,
            )

        sessions = app.get("server_sessions") or {} if hasattr(app, "get") else {}
        if not sessions:
            return ToolResult(
                output=(
                    "cockpit_show: no Mirror window is open, so there is nothing "
                    "to show. Tell the operator what to open instead."
                ),
                is_error=True,
            )

        try:
            from tesseract.mirror.server.envelope import make_envelope
            from tesseract.mirror.server.session import send_envelope
        except Exception:
            logger.exception("cockpit_show: mirror helpers import failed")
            return ToolResult(
                output="cockpit_show: Mirror envelope helpers unavailable",
                is_error=True,
            )

        payload = {
            "action": inp.action,
            "view": inp.view,
            "section": inp.section,
            "direction": inp.direction,
            "scale": inp.scale,
        }
        sent = 0
        for sess in list(sessions.values()):
            env = make_envelope(
                "cockpit_show",
                "entity",
                getattr(sess, "session_id", ""),
                payload,
            )
            try:
                await send_envelope(sess, env)
                sent += 1
            except Exception:
                logger.exception(
                    "cockpit_show: send_envelope failed for %s",
                    getattr(sess, "session_id", "?"),
                )

        if inp.action == "show":
            did = f"opened {inp.view}" + (f", {inp.section}" if inp.section else "")
        elif inp.action == "zoom":
            did = (
                f"set the text size to {inp.scale}" if inp.scale is not None
                else f"zoomed {inp.direction}"
            )
        else:
            did = f"scrolled {inp.direction}"

        if sent == 0:
            # Every window refused it, so nothing on screen moved. Reported as
            # a success the model reads "opened settings" and carries on
            # describing a panel the operator is not looking at.
            return ToolResult(
                output=(
                    f"nothing happened: no Mirror window accepted the command, "
                    f"so the app has not {did}. Check the window is still open."
                ),
                is_error=True,
                metadata={"sessions": 0, **payload},
            )

        return ToolResult(
            output=f"{did} ({sent} Mirror window(s))",
            metadata={"sessions": sent, **payload},
        )


__all__ = ["CockpitShowTool", "CockpitShowInput", "VIEWS"]
