from __future__ import annotations

import json
import pytest

from tesseract.kernel.tools.browser_tools import (
    BrowserNavigateTool, BrowserSnapshotTool, BrowserClickTool,
    BrowserFillFormTool, BrowserScreenshotTool, BrowserNetworkRequestsTool,
    BrowserCloseTool,
)
from tesseract.orchestrator.browser.manager import BrowserManager, reset_browser_manager
from tesseract.orchestrator.browser import manager as mgr_mod
from tesseract.orchestrator.browser.pc_audit import pc_audit_path
from tesseract.kernel.tools.base import ToolContext


class _FakePage:
    def __init__(self): self.url = ""
    async def goto(self, url): self.url = url
    async def accessibility_snapshot(self): return {"role": "WebArea", "name": self.url}
    async def screenshot(self, path=None):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_bytes(b"PNG")
class _FakeContext:
    async def new_page(self): return _FakePage()
    async def close(self): pass
class _FakeBrowser:
    async def new_context(self): return _FakeContext()
    async def close(self): pass
async def _fake_launcher(): return _FakeBrowser()


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_browser_manager()
    mgr_mod._manager = BrowserManager(launcher=_fake_launcher, surface_emit=False)
    yield
    reset_browser_manager()


def _ctx():
    return ToolContext(session_id="s-test")


def test_postures_match_contract():
    assert BrowserNavigateTool().default_posture == "ask"
    assert BrowserSnapshotTool().default_posture == "auto"
    assert BrowserClickTool().default_posture == "ask"
    assert BrowserFillFormTool().default_posture == "ask"
    assert BrowserScreenshotTool().default_posture == "ask"
    assert BrowserNetworkRequestsTool().default_posture == "auto"
    assert BrowserCloseTool().default_posture == "auto"


def test_names():
    assert BrowserNavigateTool().name == "browser_navigate"
    assert BrowserSnapshotTool().name == "browser_snapshot"
    assert BrowserCloseTool().name == "browser_close"


@pytest.mark.asyncio
async def test_navigate_then_snapshot_and_audit(tmp_path):
    nav = BrowserNavigateTool()
    res = await nav.run(nav.input_schema(url="https://example.com"), _ctx())
    assert not res.is_error
    cid = res.metadata["context_id"]
    snap = BrowserSnapshotTool()
    sres = await snap.run(snap.input_schema(context_id=cid), _ctx())
    assert not sres.is_error
    rows = pc_audit_path().read_text(encoding="utf-8").splitlines()
    tools = [json.loads(r)["tool"] for r in rows]
    assert "browser_navigate" in tools and "browser_snapshot" in tools


@pytest.mark.asyncio
async def test_unknown_context_is_clean_error(tmp_path):
    snap = BrowserSnapshotTool()
    res = await snap.run(snap.input_schema(context_id="nope"), _ctx())
    assert res.is_error


@pytest.mark.asyncio
async def test_navigate_reuses_session_card_by_default(tmp_path):
    """Two navigates in one session, no new_card → same context (one card)."""
    nav = BrowserNavigateTool()
    a = await nav.run(nav.input_schema(url="https://a.example"), _ctx())
    b = await nav.run(nav.input_schema(url="https://b.example"), _ctx())
    assert a.metadata["context_id"] == b.metadata["context_id"]


@pytest.mark.asyncio
async def test_navigate_new_card_forces_second_context(tmp_path):
    nav = BrowserNavigateTool()
    a = await nav.run(nav.input_schema(url="https://a.example"), _ctx())
    b = await nav.run(nav.input_schema(url="https://b.example", new_card=True), _ctx())
    assert a.metadata["context_id"] != b.metadata["context_id"]


@pytest.mark.asyncio
async def test_navigate_reuse_is_scoped_per_session(tmp_path):
    nav = BrowserNavigateTool()
    a = await nav.run(nav.input_schema(url="https://a.example"), ToolContext(session_id="s1"))
    b = await nav.run(nav.input_schema(url="https://b.example"), ToolContext(session_id="s2"))
    assert a.metadata["context_id"] != b.metadata["context_id"]


@pytest.mark.asyncio
async def test_navigate_reopens_after_close(tmp_path):
    """A closed card isn't reused — the next navigate opens a fresh one."""
    nav, close = BrowserNavigateTool(), BrowserCloseTool()
    a = await nav.run(nav.input_schema(url="https://a.example"), _ctx())
    cid = a.metadata["context_id"]
    await close.run(close.input_schema(context_id=cid), _ctx())
    b = await nav.run(nav.input_schema(url="https://b.example"), _ctx())
    assert b.metadata["context_id"] != cid
