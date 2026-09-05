"""The thirteen browser_* kernel tools. Each is a thin wrapper over
BrowserManager; the _BrowserTool base resolves the manager, runs the op,
writes a pc_audit row, and turns BrowserContextNotFound into a clean
ToolResult error. Selector-based (raw Playwright locators)."""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.browser.manager import (
    BrowserContextNotFound, get_browser_manager,
)
from tesseract.orchestrator.browser.pc_audit import append_pc_audit_row


class _BrowserTool(Tool):
    risk_class: ClassVar[str] = "propose"  # AUTO tools override to "autonomous"

    def _audit_summary(self, summary: str) -> str:
        """What the LOG records about the outcome, which is not always what
        the model is told.

        The row's `input` is redacted by the contract, and the row's
        `result_summary` sat beside it saying `pressed p`. Scrubbing one field
        of a line while its neighbour repeats the same character is a
        redaction that reads as done and is not. Overridden by the two verbs
        whose summary quotes what went into the page.
        """
        return summary

    @abstractmethod
    async def _act(self, inp: BaseModel, manager, session_id: str) -> tuple[str, dict]:
        """Return (result_summary, metadata). ``session_id`` is the calling
        session (``""`` when none) — only browser_navigate uses it, to scope
        default card-reuse."""

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        # The engine is ~700 MB and declinable, so "off" is an ordinary state
        # rather than a fault. Say which switch is false and what still works
        # — `os_open_url` hands a link to the machine's own browser and needs
        # none of this, so the alternative is one sentence away rather than a
        # reinstall. Read per call, so switching it back on takes effect on
        # the next turn.
        from tesseract.kernel.tools.web_providers.base import service_disabled_reason

        off = service_disabled_reason("browser")
        if off is not None:
            return ToolResult(
                output=(
                    f"{self.name}: the browser engine is {off} To open a link in "
                    f"the operator's own browser instead, use `open`."
                ),
                is_error=True,
            )
        manager = get_browser_manager()
        session_id = getattr(context, "session_id", "") or ""
        try:
            summary, metadata = await self._act(tool_input, manager, session_id)
        except BrowserContextNotFound as exc:
            return ToolResult(output=f"{self.name}: unknown context {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 — surface as a clean tool error
            return ToolResult(output=f"{self.name} failed: {exc}", is_error=True)
        await append_pc_audit_row(
            tool=self.name,
            input=self.redact_input(tool_input.model_dump(mode="json")),
            posture=self.default_posture,
            result_summary=self._audit_summary(summary),
            session_id=getattr(context, "session_id", "") or "",
        )
        return ToolResult(output=summary, metadata=metadata or None)


class BrowserNavigateInput(BaseModel):
    url: str = Field(description="URL to open.")
    context_id: str | None = Field(default=None, description="Specific context to navigate; omit to reuse this session's current browser card.")
    new_card: bool = Field(default=False, description="Force a separate browser card instead of reusing the session's current one. Only for showing two pages side by side.")

class BrowserNavigateTool(_BrowserTool):
    default_posture: ClassVar[str] = "ask"
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = (
        "Drive a headless browser and show a screenshot of the page on the canvas."
    )
    use_when: ClassVar[str] = (
        "You need to interact with a site: click, fill a form, log in, read the "
        "DOM. Or you need to capture how a page looks, including sites that refuse to be "
        "framed. Reuses this session's browser card by default, so a browsing "
        "flow is repeated calls rather than a pile of cards; new_card=true opens "
        "a second one on purpose. Returns context_id."
    )
    not_when: ClassVar[str] = (
        "The operator wants to SEE a link. Use `open`, which gives a live card with "
        "working controls where this gives a still image of one, and it costs a "
        "browser engine to do it. \"Show me this\" is always `open`."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_navigate"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserNavigateInput
    async def _act(self, inp, manager, session_id):
        reuse = inp.context_id or (
            None if inp.new_card else manager.last_live_context(session_id)
        )
        if reuse:
            await manager.navigate(reuse, inp.url)
            cid = reuse
        else:
            cid = await manager.open(inp.url, session_id=session_id)
        return f"navigated context {cid} to {inp.url}", {"context_id": cid}


class BrowserContextInput(BaseModel):
    context_id: str = Field(description="Browser context id.")

class BrowserSnapshotTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "looking-for-yourself"
    summary: ClassVar[str] = "Read the accessibility tree of an open browser page."
    use_when: ClassVar[str] = (
        "You need the page's structure: what elements exist and what they are for. "
        "to pick a selector or confirm something rendered."
    )
    not_when: ClassVar[str] = (
        "`browser_screenshot` for how it LOOKS; a tree cannot show you a blank pane."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_snapshot"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserContextInput
    async def _act(self, inp, manager, session_id):
        tree = await manager.snapshot(inp.context_id)
        return str(tree), {"context_id": inp.context_id}


class BrowserClickInput(BaseModel):
    context_id: str = Field(description="Browser context id.")
    selector: str = Field(description="Playwright selector (CSS, text=, role=).")

class BrowserClickTool(_BrowserTool):
    default_posture: ClassVar[str] = "ask"
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Click the element matching a selector on an open browser page."
    use_when: ClassVar[str] = (
        "You are driving a page and need to press something. Selectors are "
        "Playwright: CSS, `text=`, `role=`."
    )
    not_when: ClassVar[str] = (
        "`browser_navigate` first, which returns the context_id this takes."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_click"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserClickInput
    async def _act(self, inp, manager, session_id):
        await manager.click(inp.context_id, inp.selector)
        return f"clicked {inp.selector}", {"context_id": inp.context_id}


class _FormField(BaseModel):
    selector: str
    value: str

class BrowserFillFormInput(BaseModel):
    context_id: str = Field(description="Browser context id.")
    fields: list[_FormField] = Field(description="Selector/value pairs to fill.")

class BrowserFillFormTool(_BrowserTool):
    default_posture: ClassVar[str] = "ask"
    #: The designated way to put a value into a field, a password included.
    redacted_input_fields: ClassVar[tuple[str, ...]] = ("fields",)
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Type values into form fields on an open browser page."
    use_when: ClassVar[str] = (
        "A login, a search box, a multi-field form. One call fills every field."
    )
    not_when: ClassVar[str] = "`browser_click` presses controls; this types into them."
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_fill_form"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserFillFormInput
    async def _act(self, inp, manager, session_id):
        await manager.fill(inp.context_id, [f.model_dump() for f in inp.fields])
        return f"filled {len(inp.fields)} field(s)", {"context_id": inp.context_id}


class BrowserScreenshotTool(_BrowserTool):
    default_posture: ClassVar[str] = "ask"
    group: ClassVar[str] = "looking-for-yourself"
    summary: ClassVar[str] = "Capture an open browser page and refresh its card."
    use_when: ClassVar[str] = (
        "The page changed after a click or a fill and you need to see the result."
    )
    not_when: ClassVar[str] = (
        "The operator's own screen is `screen_look`. This only sees a page nobody is watching."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_screenshot"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserContextInput
    async def _act(self, inp, manager, session_id):
        path = await manager.screenshot(inp.context_id)
        return f"screenshot {path.name}", {"context_id": inp.context_id, "path": str(path)}


class BrowserNetworkRequestsTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "looking-for-yourself"
    summary: ClassVar[str] = "Read the network-request log of an open browser page."
    use_when: ClassVar[str] = (
        "A page misbehaves and you need what it actually asked for: a failed "
        "call, a redirect, an asset that never arrived."
    )
    not_when: ClassVar[str] = (
        "`browser_snapshot` reads the rendered result; this reads the traffic behind it."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_network_requests"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserContextInput
    async def _act(self, inp, manager, session_id):
        reqs = manager.network_requests(inp.context_id)
        return f"{len(reqs)} request(s)", {"context_id": inp.context_id, "count": len(reqs)}


class BrowserCloseTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Close a browser context and remove its card from the canvas."
    use_when: ClassVar[str] = (
        "You are finished with a page and the operator does not need its card. "
        "A context holds a browser engine open."
    )
    not_when: ClassVar[str] = (
        "Switching pages does not need it, because `browser_navigate` reuses this session's card."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_close"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserContextInput
    async def _act(self, inp, manager, session_id):
        await manager.close(inp.context_id)
        return f"closed context {inp.context_id}", {"context_id": inp.context_id}




# ── Operating a page, rather than reading one ────────────────────────────
# These six take `context_id` optionally: omitted, they act on the card this
# session is already browsing. The operator's model is "the video in front of
# me", and `last_live_context` is what makes the agent able to mean that
# without naming an id it would otherwise have to go and fetch.


class _LiveContextInput(BaseModel):
    context_id: str | None = Field(
        default=None,
        description="Which browser card to act on. Omit for the one this session is already browsing.",
    )


def _resolve(manager, inp, session_id: str) -> str:
    cid = inp.context_id or manager.last_live_context(session_id)
    if not cid:
        raise BrowserContextNotFound(
            "nothing open. Use `browser_navigate` first, or pass context_id"
        )
    return cid


class BrowserKeyInput(_LiveContextInput):
    key: str = Field(description="Playwright key name: `k`, `Enter`, `ArrowUp`, `f`, `Control+f`.")
    selector: str | None = Field(default=None, description="Focus this element first. Omit to send the key to the page.")
    repeat: int = Field(default=1, ge=1, le=20, description="Press it this many times, for volume or seek steps.")

class BrowserKeyTool(_BrowserTool):
    default_posture: ClassVar[str] = "ask"
    redacted_input_fields: ClassVar[tuple[str, ...]] = ("key",)
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Press a key on an open browser page."
    use_when: ClassVar[str] = (
        "A page is driven by the keyboard rather than by controls you can click: "
        "player shortcuts, a search box that wants Enter, a shortcut that opens a "
        "menu. `repeat` presses it several times, which is how volume and seek steps work."
    )
    not_when: ClassVar[str] = (
        "`browser_media` for play, pause, mute and volume, which reads the media "
        "element directly instead of hoping the site binds the key you guessed. "
        "`browser_fill_form` types a value into a field."
    )
    depends_on: ClassVar[str] = ""
    @classmethod
    def redact_input(cls, payload):
        """The contract's rule, with the one refinement this verb earns.

        `Enter`, `ArrowUp` and `Control+f` are what a reader needs to follow
        what happened and none of them is a secret. A single printable
        character is a keystroke, and a run of them is a password typed one
        press at a time, which is the case this exists for.
        """
        key = (payload or {}).get("key")
        if isinstance(key, str) and len(key) > 1:
            return dict(payload)
        return super().redact_input(payload)

    def _audit_summary(self, summary: str) -> str:
        # `pressed p` on the same row whose `key` was just scrubbed.
        return "pressed a key" if summary.startswith("pressed ") and len(
            summary.split(" ", 1)[1].split(" x")[0]
        ) == 1 else summary

    @property
    def name(self) -> str: return "browser_key"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserKeyInput
    async def _act(self, inp, manager, session_id):
        cid = _resolve(manager, inp, session_id)
        for _ in range(inp.repeat):
            await manager.press_key(cid, inp.key, inp.selector)
        times = "" if inp.repeat == 1 else f" x{inp.repeat}"
        return f"pressed {inp.key}{times}", {"context_id": cid}


class BrowserMediaInput(_LiveContextInput):
    # The seven the page actually answers to. A free-form string reached the
    # JS, matched no branch, and came back with the player's unchanged state,
    # which the tool reported as a successful action.
    action: Literal["play", "pause", "mute", "unmute", "volume", "seek", "read"] = Field(
        description="One of: play, pause, mute, unmute, volume, seek, read.",
    )
    value: float | None = Field(default=None, description="Volume 0 to 1 for `volume`; seconds from the start for `seek`. Ignored otherwise.")
    selector: str | None = Field(default=None, description="A specific media element. Omit for the page's first video, then its first audio.")

    @model_validator(mode="after")
    def _value_is_required_where_it_is_used(self) -> "BrowserMediaInput":
        # `volume` and `seek` are the two that read it, and JavaScript turns a
        # missing one into 0: asking to change the volume without saying to
        # what silenced the video, and a seek with no time jumped to the start.
        # Both reported success.
        if self.action in ("volume", "seek") and self.value is None:
            raise ValueError(
                f"`{self.action}` needs a `value`: "
                + ("0 to 1 for volume" if self.action == "volume" else "seconds from the start for seek")
            )
        # The page clamps out-of-range volume to 0 or 1 and reports the clamped
        # figure as the result, so asking for 50 came back reading like it had
        # worked. Refusing says which number was wrong; clamping never did.
        if self.action == "volume" and not (0.0 <= self.value <= 1.0):
            raise ValueError(f"volume is 0 to 1, not {self.value}")
        if self.action == "seek" and self.value < 0:
            raise ValueError(f"seek is seconds from the start, not {self.value}")
        return self

class BrowserMediaTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Play, pause, mute or set the volume of a video or audio on a page."
    use_when: ClassVar[str] = (
        "The operator asks you to control something playing: pause it, mute it, turn "
        "it down, skip to a time. Returns what the player now reads, so you know it "
        "worked without taking a screenshot. `read` reports without changing anything."
    )
    not_when: ClassVar[str] = (
        "`browser_key` for anything the media element itself does not do, such as "
        "fullscreen, captions or the next video."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_media"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserMediaInput
    async def _act(self, inp, manager, session_id):
        cid = _resolve(manager, inp, session_id)
        state = await manager.media(cid, inp.action, inp.value, inp.selector)
        if state is None:
            return (
                "no video or audio on this page"
                + (f" matching {inp.selector}" if inp.selector else ""),
                {"context_id": cid},
            )
        where = "paused" if state.get("paused") else "playing"
        muted = ", muted" if state.get("muted") else ""
        return (
            f"{state.get('tag')} {where}{muted}, volume {state.get('volume')}, "
            f"at {state.get('position')}s",
            {"context_id": cid, **state},
        )


class BrowserScrollInput(_LiveContextInput):
    selector: str | None = Field(default=None, description="Scroll this element into view. Omit to scroll by an offset instead.")
    dy: float = Field(default=0, description="Pixels to scroll down; negative goes up. Ignored when a selector is given.")
    dx: float = Field(default=0, description="Pixels to scroll right; negative goes left. Ignored when a selector is given.")

class BrowserScrollTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Scroll an open browser page, by an amount or to an element."
    use_when: ClassVar[str] = (
        "What you need is below the fold: a button off screen, the rest of an "
        "article, a page that loads more as you go down."
    )
    not_when: ClassVar[str] = (
        "`browser_snapshot` already reads the whole document, scrolled or not, so "
        "scrolling to READ something is wasted."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_scroll"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserScrollInput
    async def _act(self, inp, manager, session_id):
        cid = _resolve(manager, inp, session_id)
        await manager.scroll(cid, selector=inp.selector, dx=inp.dx, dy=inp.dy)
        where = f"to {inp.selector}" if inp.selector else f"by ({inp.dx}, {inp.dy})"
        return f"scrolled {where}", {"context_id": cid}


class BrowserHoverInput(_LiveContextInput):
    selector: str = Field(description="Playwright selector for the element to hover.")

class BrowserHoverTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Move the pointer over an element on an open browser page."
    use_when: ClassVar[str] = (
        "Something only appears when the pointer is over it: a drop-down menu, a "
        "chart tooltip, controls that fade in over a player."
    )
    not_when: ClassVar[str] = (
        "`browser_click` presses it. Hovering to reveal a menu is a step before "
        "clicking, not instead of it."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_hover"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserHoverInput
    async def _act(self, inp, manager, session_id):
        cid = _resolve(manager, inp, session_id)
        await manager.hover(cid, inp.selector)
        return f"hovered {inp.selector}", {"context_id": cid}


class BrowserSelectInput(_LiveContextInput):
    selector: str = Field(description="Playwright selector for the drop-down.")
    values: list[str] = Field(description="Option values to choose. More than one only for a multi-select.")

class BrowserSelectTool(_BrowserTool):
    default_posture: ClassVar[str] = "ask"
    #: An option's value is usually the page's own vocabulary, but a hidden
    #: option can carry an account id or a token, and the selector already
    #: says which control was operated.
    redacted_input_fields: ClassVar[tuple[str, ...]] = ("values",)
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Choose an option in a drop-down on an open browser page."
    use_when: ClassVar[str] = (
        "A `<select>` needs a value. Clicking one does not work reliably, because "
        "the browser draws the open list itself and it is not in the page."
    )
    not_when: ClassVar[str] = (
        "`browser_click` for a menu built out of ordinary elements, which most "
        "styled drop-downs are. Check `browser_snapshot` if you are unsure which this is."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_select"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserSelectInput
    async def _act(self, inp, manager, session_id):
        cid = _resolve(manager, inp, session_id)
        chosen = await manager.select_option(cid, inp.selector, inp.values)
        return f"selected {chosen or inp.values} in {inp.selector}", {"context_id": cid}

    def _audit_summary(self, summary: str) -> str:
        # The summary quotes the chosen option, on the same row whose
        # `values` was just scrubbed.
        _, _, where = summary.partition(" in ")
        return f"selected an option in {where}" if where else "selected an option"


class BrowserWaitForInput(_LiveContextInput):
    selector: str | None = Field(default=None, description="Wait for this element. Omit to wait for the page's network to go quiet.")
    # The four Playwright answers to. Free text reached it unchanged and threw
    # there, so a typo cost a round trip to find out.
    state: Literal["visible", "hidden", "attached", "detached"] = Field(
        default="visible",
        description="visible, hidden, attached or detached. Only with a selector.",
    )
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000, description="Give up after this long.")

class BrowserWaitForTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Wait for an element to appear on an open browser page."
    use_when: ClassVar[str] = (
        "The page has not caught up: you clicked something and the next thing is "
        "still loading. This is what stops the following call acting on a page that "
        "has not painted yet."
    )
    not_when: ClassVar[str] = (
        "`browser_snapshot` when you want to know what IS there. This only waits, "
        "and tells you whether the wait ended in time."
    )
    depends_on: ClassVar[str] = ""
    @property
    def name(self) -> str: return "browser_wait_for"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserWaitForInput
    async def _act(self, inp, manager, session_id):
        cid = _resolve(manager, inp, session_id)
        await manager.wait_for(
            cid, selector=inp.selector, state=inp.state, timeout_ms=inp.timeout_ms,
        )
        what = f"{inp.selector} to be {inp.state}" if inp.selector else "the page to go quiet"
        return f"waited for {what}", {"context_id": cid}


__all__ = [
    "BrowserNavigateTool", "BrowserSnapshotTool", "BrowserClickTool",
    "BrowserFillFormTool", "BrowserScreenshotTool",
    "BrowserNetworkRequestsTool", "BrowserCloseTool",
    "BrowserKeyTool", "BrowserMediaTool", "BrowserScrollTool",
    "BrowserHoverTool", "BrowserSelectTool", "BrowserWaitForTool",
]
