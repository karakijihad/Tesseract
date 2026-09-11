"""surface_control — operate a card the operator is actually looking at.

`surface_create` draws a card and `surface_update` redraws it. Neither can ask
a card to DO something: to pause, to turn down, to press the button inside a
page the assistant itself wrote. Redrawing a live thing is not operating it,
and for a game it throws away the state the operator was mid-way through.

**One verb, and the card kind knows how to obey.** A local video is a real
`<video>` in the app's own DOM. A YouTube or Vimeo card is a player with an
API. An `html` card carries a bridge the app injects into every page it draws,
so an authored page obeys because it was drawn, not because someone remembered
to write the listener. Three mechanisms, one thing being asked, so the model is
not made to learn which is which.

**What it deliberately cannot reach.** An arbitrary website in a card is
sandboxed with no API to talk to, and it says so instead of pretending. The
sentence matters: `browser_media`'s error used to send the model to
`browser_navigate`, and the model obeyed it and opened a second copy of the
operator's video in a headless browser they could neither see nor hear.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.commands import (
    DEFAULT_TIMEOUT_S,
    get_command_broker,
)
from tesseract.orchestrator.surfaces.events import publish_surface_event
from tesseract.orchestrator.surfaces.store import get_surface_store

logger = logging.getLogger(__name__)

#: Card kinds that answer a command, and what a reader should expect of each.
#: A kind absent from here is not broken, it is simply not operable, and the
#: tool says which one it hit rather than timing out and blaming the network.
OPERABLE: dict[str, str] = {
    "video": "a video playing in the operator's window",
    "audio": "audio playing in the operator's window",
    "webview": "an embedded player, when the page is one that offers an API",
    "iframe": "an embedded player, when the page is one that offers an API",
    "html": "a page the assistant authored, which always carries the bridge",
}

MEDIA_ACTIONS = ("play", "pause", "mute", "unmute", "volume", "seek", "read")


class SurfaceControlInput(BaseModel):
    surface_id: str = Field(description="The card to operate. `surface_list` names them.")
    action: Literal[
        "play", "pause", "mute", "unmute", "volume", "seek", "read", "press", "set"
    ] = Field(
        description=(
            "play/pause/mute/unmute/volume/seek operate a video, audio or "
            "embedded player. press and set act on a page you authored. read "
            "reports state and changes nothing."
        ),
    )
    value: float | str | None = Field(
        default=None,
        description=(
            "Volume 0 to 1, seconds from the start for seek, or the value for "
            "`set`. Not used by play, pause, mute, unmute or read."
        ),
    )
    target: str | None = Field(
        default=None,
        description=(
            "For press and set on an authored page: which control, by the "
            "name the page gave it. `read` lists those names. Omit for media, "
            "which addresses the card's own player."
        ),
    )


class SurfaceControlTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "showing-the-operator"
    summary: ClassVar[str] = "Operate a card on the canvas: play, pause, volume, or press its controls."
    use_when: ClassVar[str] = (
        "Something is on the operator's canvas and they want it operated rather "
        "than replaced: pause the video, turn the song down, deal the next round "
        "of a game you built. Returns what the card then reads, so you know it "
        "worked without asking for a screenshot."
    )
    not_when: ClassVar[str] = (
        "`surface_update` changes what a card SHOWS, which restarts whatever was "
        "running in it. `browser_media` operates a page in your own headless "
        "browser, which the operator cannot see or hear, so it is never the "
        "answer for something already on their canvas."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "queryable"

    @property
    def name(self) -> str:
        return "surface_control"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceControlInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceControlInput = tool_input  # type: ignore[assignment]
        store = get_surface_store()
        card = store.get(inp.surface_id)
        if card is None:
            return ToolResult(
                output=(
                    f"surface_control: no card {inp.surface_id!r} on the canvas. "
                    f"`surface_list` names what is there."
                ),
                is_error=True,
            )

        kind = str(card.get("type") or "")
        if kind not in OPERABLE:
            return ToolResult(
                output=(
                    f"surface_control: a {kind!r} card has nothing to operate. "
                    f"Operable kinds are: {', '.join(sorted(OPERABLE))}."
                ),
                is_error=True,
            )

        problem = self._validate(inp, kind)
        if problem is not None:
            return ToolResult(output=problem, is_error=True)

        broker = get_command_broker()
        command_id, fut = broker.issue()
        publish_surface_event(
            kind="surface_command",
            view=str(card.get("view") or "orb"),
            data={
                "command_id": command_id,
                "surface_id": inp.surface_id,
                "action": inp.action,
                "value": inp.value,
                "target": inp.target,
            },
        )

        answer = await broker.wait(command_id, fut, DEFAULT_TIMEOUT_S)
        if answer is None:
            # Two real causes now, and the operator can tell them apart from
            # what is in front of them: no Mirror window open, or the card was
            # closed a moment ago. A card that is there answers, even to
            # refuse, because every adapter answers.
            return ToolResult(
                output=(
                    f"surface_control: {inp.action} went to {inp.surface_id} and "
                    f"nothing answered. The window may be closed, or the card "
                    f"was closed a moment ago. Ask the operator what they see."
                ),
                is_error=True,
            )
        if not answer.get("ok"):
            reason = str(answer.get("error") or "the card refused it")
            remedy = _remedy(kind, reason)
            return ToolResult(
                output=f"surface_control: {inp.action} failed. {reason}{remedy}",
                is_error=True,
            )

        state = answer.get("state") or {}
        output = self._describe(inp.action, kind, state)
        metadata: dict[str, Any] = {"surface_id": inp.surface_id, "type": kind, **state}
        if inp.action == "read":
            # The other half of the bridge, and the only place it is legible.
            # A card that was pressed while the assistant was not looking has
            # been keeping that fact; reading the card is when it hands it
            # over.
            events = store.card_events(inp.surface_id)
            output = output + "\n" + _event_lines(events)
            metadata["events"] = events
        return ToolResult(output=output, metadata=metadata)

    def _validate(self, inp: SurfaceControlInput, kind: str) -> str | None:
        if inp.action == "volume" and not isinstance(inp.value, (int, float)):
            return "surface_control: `volume` needs a number between 0 and 1."
        if inp.action == "seek" and not isinstance(inp.value, (int, float)):
            return "surface_control: `seek` needs a position in seconds."
        if inp.action in ("press", "set"):
            if kind != "html":
                return (
                    f"surface_control: `{inp.action}` is for a page you authored. "
                    f"A {kind!r} card takes {', '.join(MEDIA_ACTIONS)}."
                )
            if not inp.target:
                return f"surface_control: `{inp.action}` needs a target control."
        if inp.action in MEDIA_ACTIONS and kind == "html" and inp.action != "read":
            return (
                "surface_control: an authored page takes `press` and `set`, not "
                "player controls, unless it renders its own media element."
            )
        return None

    @staticmethod
    def _describe(action: str, kind: str, state: dict[str, Any]) -> str:
        if not state:
            return f"{action} done on the {kind} card"
        if "paused" in state:
            where = "paused" if state.get("paused") else "playing"
            muted = ", muted" if state.get("muted") else ""
            at = state.get("position")
            tail = f", at {round(float(at), 1)}s" if isinstance(at, (int, float)) else ""
            return f"{where}{muted}, volume {state.get('volume')}{tail}"
        if "controls" in state:
            controls = state.get("controls") or []
            head = "the page" if action == "read" else f"{action} done"
            parts = [f"{head} takes: " + (", ".join(controls) if controls else "nothing")]
            values = state.get("values") or {}
            if values:
                parts.append("values: " + ", ".join(f"{k}={v}" for k, v in values.items()))
            page = state.get("page")
            if page is not None:
                parts.append(f"the page reports: {page}")
            return ". ".join(parts)
        return f"{action} done: {state}"


def _event_lines(events: list[dict[str, Any]]) -> str:
    """What the operator did inside the card since it drew, oldest first."""
    if not events:
        return "Nothing has been pressed or typed in it since it drew."
    rows = []
    for e in events:
        value = e.get("value")
        tail = f" = {value}" if value is not None else ""
        rows.append(f"  {e.get('at')} {e.get('event')} {e.get('target')}{tail}")
    header = f"{len(events)} thing(s) done in it since it drew, oldest first:"
    return "\n".join([header, *rows])


def _remedy(kind: str, reason: str) -> str:
    """What to do about a card that would not obey, which differs by whose
    card it is.

    A refusal that only states the fact leaves the model to invent a next
    move, and the invented move has already been the wrong one once: told to
    pause a video it could not reach, it opened a second copy in a headless
    browser the operator could neither see nor hear. So each case names its
    own way forward, including the cases where the way forward is to stop.
    """
    if kind == "html":
        # An authored page is the one case that is genuinely self-service, and
        # never a case for the headless copy below: the assistant wrote this
        # page, so a page that stopped answering is one whose own script threw
        # the document away, and redrawing it brings the listener back. A
        # refusal that already names the page's controls needs nothing added.
        if "did not answer" not in reason:
            return ""
        return (
            " You authored this page, so the fix is yours: its script replaced "
            "the whole document, which threw away the part that listens. "
            "Redraw it with `surface_update`, building the page inside the body "
            "it is given, and it obeys again."
        )
    if "no way to control it" not in reason:
        return ""
    return (
        " Nothing reaches a page that publishes no way in. Say so plainly, and "
        "offer what is actually available: the operator can use the card's own "
        "controls, or you can open your own copy with `browser_navigate` and "
        "drive that, which they will not see or hear. Do not open a copy "
        "without telling them it is a copy."
    )


__all__ = ["SurfaceControlTool", "SurfaceControlInput", "OPERABLE"]
