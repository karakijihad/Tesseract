"""orb_visibility — TARS shows or hides its orb in the Mirror cockpit.

The orb is TARS's body on screen. The operator can hide it via the HUD
toggle; this tool gives TARS the same control from chat or voice ("hide
yourself", "come back"). Hiding also pauses the WebGL render loop
(GlobalCanvas pause/resume), so this doubles as a way to shed GPU/CPU
load on request.

Pushes an ``orb_visibility`` entity envelope to every open Mirror WS
session — same broadcast pattern as ``chat_initiate``. The frontend
writes the same store as the HUD toggle (``stores/orbVisibility.ts``),
so the two controls never diverge.

``default_posture="auto"`` — pure UI signal, no I/O; matches ``set_state``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)


class OrbVisibilityInput(BaseModel):
    visible: bool = Field(
        description=(
            "true shows the orb, false hides it (and pauses its render "
            "loop). Applies to every open Mirror window and persists "
            "across reloads, exactly like the operator's HUD toggle."
        ),
    )


class OrbVisibilityTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        """``app_provider`` resolves the Mirror ``web.Application`` at call
        time (closure pattern; matches chat_initiate). REPL / unit tests
        pass ``None`` → the tool refuses with a clear error rather than
        crashing.
        """
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "orb_visibility"

    @property
    def description(self) -> str:
        return (
            "Show or hide your orb in the Mirror cockpit. Hiding pauses "
            "the orb's render loop (frees GPU/CPU); showing restores it. "
            "Use when the operator asks you to hide/show yourself, or to "
            "shed load. Same switch as the operator's HUD toggle."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return OrbVisibilityInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, OrbVisibilityInput)
            else OrbVisibilityInput(**tool_input.model_dump())
        )

        app = self._app_provider() if self._app_provider is not None else None
        if app is None:
            return ToolResult(
                output=(
                    "orb_visibility: no Mirror app context (running headless "
                    "or test) — there is no orb to control here."
                ),
                is_error=True,
            )

        sessions = app.get("server_sessions") or {} if hasattr(app, "get") else {}
        if not sessions:
            return ToolResult(
                output="orb_visibility: no Mirror window open — nothing to push to.",
                is_error=True,
            )

        # Late import — same fail-soft pattern as chat_initiate.
        try:
            from tesseract.mirror.server.envelope import make_envelope
            from tesseract.mirror.server.session import send_envelope
        except Exception:
            logger.exception("orb_visibility: mirror helpers import failed")
            return ToolResult(
                output="orb_visibility: Mirror envelope helpers unavailable",
                is_error=True,
            )

        payload = {"visible": inp.visible}
        sent = 0
        for sess in list(sessions.values()):
            env = make_envelope(
                "orb_visibility",
                "entity",
                getattr(sess, "session_id", ""),
                payload,
            )
            try:
                await send_envelope(sess, env)
                sent += 1
            except Exception:
                logger.exception(
                    "orb_visibility: send_envelope failed for %s",
                    getattr(sess, "session_id", "?"),
                )

        verb = "shown" if inp.visible else "hidden"
        return ToolResult(
            output=f"orb {verb} ({sent} Mirror session(s))",
            metadata={"sessions": sent, "visible": inp.visible},
        )


__all__ = ["OrbVisibilityTool", "OrbVisibilityInput"]
