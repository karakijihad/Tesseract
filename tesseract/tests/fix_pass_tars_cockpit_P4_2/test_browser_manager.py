from __future__ import annotations

import pytest

from tesseract.orchestrator.browser.manager import (
    BrowserManager, BrowserContextNotFound, reset_browser_manager,
)


class _FakePage:
    def __init__(self) -> None:
        self.url = ""
        self.requests: list[dict] = []
        self.closed = False
    async def goto(self, url): self.url = url
    async def accessibility_snapshot(self): return {"role": "WebArea", "name": self.url}
    async def click(self, selector): self.clicked = selector
    async def fill(self, selector, value): self.filled = (selector, value)
    async def screenshot(self, path=None):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"PNG")
        return b"PNG"


class _FakeContext:
    def __init__(self): self.pages = []; self.closed = False
    async def new_page(self): p = _FakePage(); self.pages.append(p); return p
    async def close(self): self.closed = True


class _FakeBrowser:
    def __init__(self): self.contexts = []; self.closed = False
    async def new_context(self): c = _FakeContext(); self.contexts.append(c); return c
    async def close(self): self.closed = True


async def _fake_launcher():
    return _FakeBrowser()


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    reset_browser_manager()
    yield
    reset_browser_manager()


@pytest.mark.asyncio
async def test_open_creates_context_and_navigates(tmp_path):
    mgr = BrowserManager(launcher=_fake_launcher, surface_emit=False)
    cid = await mgr.open("https://example.com")
    assert isinstance(cid, str) and cid
    snap = await mgr.snapshot(cid)
    assert snap["name"] == "https://example.com"


@pytest.mark.asyncio
async def test_open_auto_captures_first_shot(tmp_path):
    # `browser_navigate` alone must render the page into the canvas: open()
    # captures a screenshot so the image card points at a shot that exists
    # (not a 404) without a separate browser_screenshot call.
    from tesseract.orchestrator.browser.manager import _browser_root
    mgr = BrowserManager(launcher=_fake_launcher, surface_emit=False)
    cid = await mgr.open("https://example.com")
    assert (_browser_root() / cid / "1.png").exists()


@pytest.mark.asyncio
async def test_navigate_auto_captures_next_shot(tmp_path):
    from tesseract.orchestrator.browser.manager import _browser_root
    mgr = BrowserManager(launcher=_fake_launcher, surface_emit=False)
    cid = await mgr.open("https://example.com")
    await mgr.navigate(cid, "https://example.org")
    assert (_browser_root() / cid / "2.png").exists()


@pytest.mark.asyncio
async def test_screenshot_writes_png_under_tesseract_home(tmp_path):
    mgr = BrowserManager(launcher=_fake_launcher, surface_emit=False)
    cid = await mgr.open("https://example.com")
    path = await mgr.screenshot(cid)
    assert path.exists()
    assert str(tmp_path) in str(path)


@pytest.mark.asyncio
async def test_unknown_context_raises_typed_error():
    mgr = BrowserManager(launcher=_fake_launcher, surface_emit=False)
    with pytest.raises(BrowserContextNotFound):
        await mgr.snapshot("nope")


@pytest.mark.asyncio
async def test_close_then_shutdown(tmp_path):
    mgr = BrowserManager(launcher=_fake_launcher, surface_emit=False)
    cid = await mgr.open("https://example.com")
    await mgr.close(cid)
    with pytest.raises(BrowserContextNotFound):
        await mgr.snapshot(cid)
    await mgr.shutdown()  # idempotent, no raise


@pytest.mark.asyncio
async def test_concurrent_open_launches_one_browser(tmp_path):
    import asyncio
    launches = {"n": 0}
    async def _counting_launcher():
        launches["n"] += 1
        return _FakeBrowser()
    mgr = BrowserManager(launcher=_counting_launcher, surface_emit=False)
    await asyncio.gather(mgr.open("https://a.test"), mgr.open("https://b.test"))
    assert launches["n"] == 1
