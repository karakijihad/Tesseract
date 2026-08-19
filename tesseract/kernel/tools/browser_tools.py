"""P4-2 — the seven browser_* kernel tools. Each is a thin wrapper over
BrowserManager; the _BrowserTool base resolves the manager, runs the op,
writes a pc_audit row, and turns BrowserContextNotFound into a clean
ToolResult error. Selector-based (raw Playwright locators)."""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.browser.manager import (
    BrowserContextNotFound, get_browser_manager,
)
from tesseract.orchestrator.browser.pc_audit import append_pc_audit_row


class _BrowserTool(Tool):
    risk_class: ClassVar[str] = "propose"  # AUTO tools override to "autonomous"

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
            input=tool_input.model_dump(mode="json"),
            posture=self.default_posture,
            result_summary=summary,
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
        "You need to interact with a site — click, fill a form, log in, read the "
        "DOM — or to capture how a page looks, including sites that refuse to be "
        "framed. Reuses this session's browser card by default, so a browsing "
        "flow is repeated calls rather than a pile of cards; new_card=true opens "
        "a second one on purpose. Returns context_id."
    )
    not_when: ClassVar[str] = (
        "The operator wants to SEE a link. Use `open` — it gives a live card with "
        "working controls where this gives a still image of one, and it costs a "
        "browser engine to do it. \"Show me this\" is always `open`."
    )
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
        "You need the page's structure — what elements exist and their roles — "
        "to pick a selector or confirm something rendered."
    )
    not_when: ClassVar[str] = (
        "`browser_screenshot` for how it LOOKS; a tree cannot show you a blank pane."
    )
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
        "`browser_navigate` first — it returns the context_id this takes."
    )
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
    group: ClassVar[str] = "driving-a-web-page"
    summary: ClassVar[str] = "Type values into form fields on an open browser page."
    use_when: ClassVar[str] = (
        "A login, a search box, a multi-field form. One call fills every field."
    )
    not_when: ClassVar[str] = "`browser_click` presses controls; this types into them."
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
        "The operator's own screen is `screen_look` — this only sees a headless page."
    )
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
        "A page misbehaves and you need what it actually asked for — a failed "
        "call, a redirect, an asset that never arrived."
    )
    not_when: ClassVar[str] = (
        "`browser_snapshot` reads the rendered result; this reads the traffic behind it."
    )
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
        "Switching pages does not need it — `browser_navigate` reuses this session's card."
    )
    @property
    def name(self) -> str: return "browser_close"
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserContextInput
    async def _act(self, inp, manager, session_id):
        await manager.close(inp.context_id)
        return f"closed context {inp.context_id}", {"context_id": inp.context_id}


__all__ = [
    "BrowserNavigateTool", "BrowserSnapshotTool", "BrowserClickTool",
    "BrowserFillFormTool", "BrowserScreenshotTool",
    "BrowserNetworkRequestsTool", "BrowserCloseTool",
]
