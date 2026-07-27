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
    @property
    def name(self) -> str: return "browser_navigate"
    @property
    def description(self) -> str:
        return (
            "Open a URL in a headless browser and render it as a live image card "
            "on the Mirror canvas (auto-captures a screenshot on open). This is how "
            "you SHOW ANY WEBSITE in-canvas — including ones that refuse to embed in "
            "a webview surface (Google, LinkedIn, X, banks, most logged-in sites). "
            "By DEFAULT this REUSES the session's current browser card (navigating "
            "it to the new URL) — call it repeatedly for a browsing flow without "
            "piling up cards. Pass context_id to target a specific card, or "
            "new_card=true to open a second one on purpose. "
            "Use browser_screenshot to refresh it. Returns context_id."
        )
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
    @property
    def name(self) -> str: return "browser_snapshot"
    @property
    def description(self) -> str: return "Read the accessibility-tree snapshot of the context's current page (read-only)."
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
    @property
    def name(self) -> str: return "browser_click"
    @property
    def description(self) -> str: return "Click the element matching the Playwright selector."
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
    @property
    def name(self) -> str: return "browser_fill_form"
    @property
    def description(self) -> str: return "Fill form fields by Playwright selector."
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserFillFormInput
    async def _act(self, inp, manager, session_id):
        await manager.fill(inp.context_id, [f.model_dump() for f in inp.fields])
        return f"filled {len(inp.fields)} field(s)", {"context_id": inp.context_id}


class BrowserScreenshotTool(_BrowserTool):
    default_posture: ClassVar[str] = "ask"
    @property
    def name(self) -> str: return "browser_screenshot"
    @property
    def description(self) -> str: return "Capture the context's viewport and refresh its cockpit card."
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserContextInput
    async def _act(self, inp, manager, session_id):
        path = await manager.screenshot(inp.context_id)
        return f"screenshot {path.name}", {"context_id": inp.context_id, "path": str(path)}


class BrowserNetworkRequestsTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str: return "browser_network_requests"
    @property
    def description(self) -> str: return "Read the captured network-request log for the context (read-only)."
    @property
    def input_schema(self) -> type[BaseModel]: return BrowserContextInput
    async def _act(self, inp, manager, session_id):
        reqs = manager.network_requests(inp.context_id)
        return f"{len(reqs)} request(s)", {"context_id": inp.context_id, "count": len(reqs)}


class BrowserCloseTool(_BrowserTool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str: return "browser_close"
    @property
    def description(self) -> str: return "Close a browser context and remove its cockpit card."
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
