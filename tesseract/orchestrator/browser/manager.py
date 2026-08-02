"""P4-2 — BrowserManager: a headless Playwright Chromium with N isolated
contexts, each keyed by a short context_id and mirrored as an `image`
surface card. Best-effort reflection; the Playwright launcher is injectable
so tests run against a fake (no real browser, no chromium download)."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class BrowserContextNotFound(Exception):
    """Raised when a tool references a context_id the manager doesn't hold."""


def _browser_root() -> Path:
    override = os.environ.get("TESSERACT_HOME")
    from tesseract.paths import TESSERACT_HOME, runtime_dir
    home = Path(override).resolve() if override else TESSERACT_HOME
    return runtime_dir() / "browser"


async def _default_launcher():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    browser._tars_pw = pw  # type: ignore[attr-defined]
    return browser


@dataclass
class _Ctx:
    context: Any
    page: Any
    surface_id: str | None = None
    shots: int = 0
    requests: list[dict] = field(default_factory=list)


class BrowserManager:
    def __init__(
        self,
        *,
        launcher: Callable[[], Awaitable[Any]] | None = None,
        surface_emit: bool = True,
    ) -> None:
        self._launcher = launcher or _default_launcher
        self._surface_emit = surface_emit
        self._browser: Any | None = None
        self._ctxs: dict[str, _Ctx] = {}
        # Most-recently-opened context per session, so browser_navigate can
        # reuse a session's current card by default instead of spawning a
        # new one on every call (the "card spam" fix, 2026-07-17). Keyed by
        # session_id ("" for headless/no-session); a session browsing in a
        # different chat never reuses another's card.
        self._last_by_session: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def _ensure_browser(self) -> Any:
        if self._browser is None:
            async with self._lock:
                if self._browser is None:
                    self._browser = await self._launcher()
        return self._browser

    def _ctx(self, cid: str) -> _Ctx:
        c = self._ctxs.get(cid)
        if c is None:
            raise BrowserContextNotFound(cid)
        return c

    async def open(self, url: str, session_id: str = "") -> str:
        browser = await self._ensure_browser()
        context = await browser.new_context()
        page = await context.new_page()
        cid = uuid.uuid4().hex[:8]
        rec = _Ctx(context=context, page=page)
        self._ctxs[cid] = rec
        self._last_by_session[session_id] = cid
        await page.goto(url)
        # Capture BEFORE reflecting so the image card points at a shot that
        # exists — otherwise the card renders a 404 until browser_screenshot
        # is called separately. This is what makes `browser_navigate` alone
        # show the page in the canvas (incl. sites that refuse to iframe).
        await self._capture(cid)
        await self._refresh_surface(cid, url, create=True)
        return cid

    def last_live_context(self, session_id: str = "") -> str | None:
        """The session's most-recent context if it's still open, else None.
        Prunes the pointer when the context has since been closed."""
        cid = self._last_by_session.get(session_id)
        if cid and cid in self._ctxs:
            return cid
        if cid is not None:
            self._last_by_session.pop(session_id, None)
        return None

    async def navigate(self, cid: str, url: str) -> None:
        c = self._ctx(cid)
        await c.page.goto(url)
        await self._capture(cid)
        await self._refresh_surface(cid, url, create=False)

    async def snapshot(self, cid: str) -> dict | None:
        c = self._ctx(cid)
        acc = getattr(c.page, "accessibility", None)
        if acc is not None and hasattr(acc, "snapshot"):
            return await acc.snapshot()
        return await c.page.accessibility_snapshot()

    async def click(self, cid: str, selector: str) -> None:
        c = self._ctx(cid)
        if hasattr(c.page, "locator"):
            await c.page.locator(selector).click()
        else:
            await c.page.click(selector)

    async def fill(self, cid: str, fields: list[dict]) -> None:
        c = self._ctx(cid)
        for f in fields:
            sel, val = f["selector"], f["value"]
            if hasattr(c.page, "locator"):
                await c.page.locator(sel).fill(val)
            else:
                await c.page.fill(sel, val)

    async def _capture(self, cid: str) -> Path:
        """Write the next screenshot for the context. Shared by open/navigate
        (auto-capture so the card is never blank) and the public screenshot()."""
        c = self._ctx(cid)
        c.shots += 1
        path = _browser_root() / cid / f"{c.shots}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await c.page.screenshot(path=str(path))
        return path

    async def screenshot(self, cid: str) -> Path:
        path = await self._capture(cid)
        c = self._ctx(cid)
        await self._refresh_surface(cid, getattr(c.page, "url", ""), create=False)
        return path

    def network_requests(self, cid: str) -> list[dict]:
        return list(self._ctx(cid).requests)

    async def close(self, cid: str) -> None:
        c = self._ctxs.pop(cid, None)
        if c is None:
            raise BrowserContextNotFound(cid)
        for sess, last in list(self._last_by_session.items()):
            if last == cid:
                self._last_by_session.pop(sess, None)
        if self._surface_emit and c.surface_id:
            self._safe_surface(lambda s: s.close(c.surface_id))
        try:
            await c.context.close()
        except Exception:  # noqa: BLE001
            log.warning("browser: context close failed for %s", cid, exc_info=True)

    async def shutdown(self) -> None:
        for cid in list(self._ctxs):
            try:
                await self.close(cid)
            except Exception:  # noqa: BLE001
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
                pw = getattr(self._browser, "_tars_pw", None)
                if pw is not None:
                    await pw.stop()
            except Exception:  # noqa: BLE001
                log.warning("browser: shutdown failed", exc_info=True)
            self._browser = None

    async def _refresh_surface(self, cid: str, url: str, *, create: bool) -> None:
        if not self._surface_emit:
            return
        c = self._ctx(cid)
        asset = f"/api/browser-assets/{cid}/{max(c.shots,1)}.png"
        host = urlparse(url).netloc or url
        if create or not c.surface_id:
            sid = self._safe_surface(lambda s: s.create(
                type="image", view="tars",
                props={"context_id": cid, "url": asset},
                title=f"Browser — {host}", mode="embedded",
            ))
            if sid:
                c.surface_id = sid
        else:
            self._safe_surface(lambda s: s.update(c.surface_id, props={"url": asset}))

    def _safe_surface(self, fn):
        try:
            from tesseract.orchestrator.surfaces.store import get_surface_store
            return fn(get_surface_store())
        except Exception:  # noqa: BLE001 — reflection must never break a browser op
            log.warning("browser: surface reflect failed", exc_info=True)
            return None


_manager: BrowserManager | None = None


def get_browser_manager() -> BrowserManager:
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager


def reset_browser_manager() -> None:
    global _manager
    _manager = None


__all__ = [
    "BrowserManager", "BrowserContextNotFound",
    "get_browser_manager", "reset_browser_manager",
]
