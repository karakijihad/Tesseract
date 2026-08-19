"""Photograph the display the app is on.

Reads the **composited desktop** (`PIL.ImageGrab`) rather than asking a window
to redraw itself into a bitmap. `PrintWindow` / `BitBlt` against a
WebView2-backed window commonly returns a BLACK image — the same symptom that
started this line of work — while the desktop is already composited by the
window manager, so what is on the glass is what gets read. It is also faster,
because nothing re-renders, and it needs no Rust and no new installer.

**One frame, one meaning: the display the app window sits on.** This module
used to crop to the window rectangle. That sounds narrower, and was, but a
window is only croppable while it is on the glass — minimised, closed or
off-screen it is not. Each of those needed a fallback; the fallback needed a
scope; the scope needed a caveat riding on the answer; and a title that is not
identity needed an owner check to caption that caveat. None of it was the
picture. Operator, 2026-08-16: *"taking a screenshot of a screen is taking it,
and that's it."*

The cost is stated rather than mitigated: anything else open on that display is
in the frame. That is what the operator is looking at, and `screen_look` asks
before every call.

The frame is never written to disk. It is a photograph of the operator's
screen — it can hold a key, a private conversation, an unrelated
application — so the bytes go to the vision model from memory and are dropped.

`Pillow` does the grab. It is a declared dependency (`pyproject.toml`) rather
than the transitive it used to be, because a missing one here is the assistant
silently losing its eyes.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

# The Tauri shell's window title (`src-tauri/tauri.conf.json::app.windows[].title`),
# matched as a PREFIX. An exact full-title match found the packaged window and
# nothing else: a dev build in a browser is "TESSERACT - Google Chrome".
WINDOW_TITLE = "TESSERACT"

# A browser appends its own name after a dash, which is the only thing that
# may follow the title. Without the boundary, an operator's file named
# `TESSERACT notes.docx` open in Word wins the match and picks the display.
_TITLE_SUFFIXES = (" - ", " – ", " — ")


@dataclass(frozen=True)
class Capture:
    """A frame, and the name of the display it came from.

    `png` carries the bytes, so the file never has to exist for the frame to be
    usable — and it never does exist.
    """

    png: bytes
    width: int
    height: int
    # Device name of the display captured ("DISPLAY1 (primary)"), or "" when
    # Windows could not name one. Travels to the vision prompt so the answer
    # never claims to be about a display it is not.
    monitor: str = ""


_dpi_attempted = False


def _make_dpi_aware() -> None:
    """Ask Windows for physical pixels.

    Without this, window and monitor rectangles come back in logical
    coordinates while the desktop grab is in physical ones, so on any scaled
    display the crop lands somewhere other than the display (2048x1152 for a
    2560x1440 screen at 125%). Guarded and attempted once: this backend draws
    nothing, so raising awareness has no rendering consequence here, and a
    failure is not worth aborting a capture over.
    """
    global _dpi_attempted
    if _dpi_attempted or sys.platform != "win32":
        return
    _dpi_attempted = True
    import ctypes

    # Logged at INFO on success, not only on failure. This is a process-wide,
    # irreversible OS setting changed as a side effect of a tool call, and a
    # mutation nobody can see in the record is one nobody can explain later.
    try:
        # Per-monitor-v2 where available, process-wide otherwise.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        log.info("screen capture: raised process DPI awareness (per-monitor v2)")
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            log.info("screen capture: raised process DPI awareness (system)")
        except Exception:
            log.debug("screen capture: could not raise DPI awareness", exc_info=True)


def _titled_windows() -> list[tuple[int, str]]:
    """Every visible top-level window that has a title, front-most first.

    Enumerating is what makes the match a prefix rather than an exact string.
    `EnumWindows` yields z-order, so the first match is the one in front.
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    # Declared rather than inferred: an undeclared ctypes call marshals a
    # Python int as a 32-bit c_int, which truncates a 64-bit handle into a
    # handle for nothing.
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _collect(hwnd, _lparam):  # type: ignore[no-untyped-def]
        handle = int(hwnd or 0)
        if not handle or not user32.IsWindowVisible(wintypes.HWND(handle)):
            return True
        length = user32.GetWindowTextLengthW(wintypes.HWND(handle))
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(wintypes.HWND(handle), buf, length + 1)
        if buf.value:
            found.append((handle, buf.value))
        return True

    try:
        user32.EnumWindows(proc(_collect), 0)
    except Exception:
        log.debug("screen capture: could not enumerate windows", exc_info=True)
    return found


def app_window(prefix: str = WINDOW_TITLE) -> int:
    """HWND of the front-most window titled `prefix`, or 0.

    The title is either the prefix exactly (the packaged shell) or the prefix
    followed by a browser's own name (a dev build). Front-most and nothing
    else. There is no owner check because there is nothing left for it to
    qualify: the frame is a display either way, so knowing which process drew
    a window on it changes no part of the answer.
    """
    if sys.platform != "win32":
        return 0
    _make_dpi_aware()
    for hwnd, title in _titled_windows():
        if title == prefix or title.startswith(tuple(prefix + s for s in _TITLE_SUFFIXES)):
            return hwnd
    return 0


def app_monitor() -> tuple[tuple[int, int, int, int] | None, str]:
    """Bounding box and device name of the display the app window sits on.

    Resolved from `GetWindowPlacement`'s restored rectangle rather than the
    live one, because that is the single value that is meaningful whether the
    window is on the glass or minimised — a minimised window's live rect is
    parked off-screen at -32000 and would resolve to whichever display is
    nearest to nowhere. No window at all falls back to the primary display.

    `(None, "")` off Windows and when the query fails; the caller then takes
    Pillow's own default, which is one display.
    """
    if sys.platform != "win32":
        return None, ""
    import ctypes
    from ctypes import wintypes

    MONITOR_DEFAULTTOPRIMARY = 1
    MONITOR_DEFAULTTONEAREST = 2
    MONITORINFOF_PRIMARY = 1

    class _WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.UINT),
            ("flags", wintypes.UINT),
            ("showCmd", wintypes.UINT),
            ("ptMinPosition", wintypes.POINT),
            ("ptMaxPosition", wintypes.POINT),
            ("rcNormalPosition", wintypes.RECT),
        ]

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    _make_dpi_aware()
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE

    try:
        handle = None
        hwnd = app_window()
        if hwnd:
            placement = _WINDOWPLACEMENT()
            placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
            if user32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(placement)):
                r = placement.rcNormalPosition
                centre = wintypes.POINT((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                handle = user32.MonitorFromPoint(centre, MONITOR_DEFAULTTONEAREST)
        if not handle:
            handle = user32.MonitorFromPoint(
                wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY
            )
        if not handle:
            return None, ""
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return None, ""
        rect = info.rcMonitor
        name = info.szDevice.rsplit("\\", 1)[-1] or "display"
        if info.dwFlags & MONITORINFOF_PRIMARY:
            name = f"{name} (primary)"
        return (rect.left, rect.top, rect.right, rect.bottom), name
    except Exception:
        log.debug("screen capture: could not resolve a display", exc_info=True)
        return None, ""


def _grab() -> Capture:
    """Synchronous capture. Run it off the loop — see `capture_screen`."""
    from PIL import ImageGrab

    bbox, monitor = app_monitor()
    # `all_screens=True` so a display at negative virtual-screen coordinates
    # (any monitor left of or above the primary) is reachable at all.
    image = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab()

    image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Capture(
        png=buffer.getvalue(),
        width=image.width,
        height=image.height,
        monitor=monitor,
    )


async def capture_screen() -> Capture:
    """Capture the display the app window is on, at its native resolution.

    Nothing is written to disk and nothing is scaled down. The frame used to be
    resized to a configured longest edge to hold the cost of a look down; image
    tokens do scale with area, but on the flash-tier role that bills this the
    difference is a rounding error per call, and the resolution it cost is the
    difference between reading an error message on screen and guessing at it.

    Off the loop: a desktop grab plus a PNG encode is tens of milliseconds,
    over the 50 ms budget that keeps health checks and WS heartbeats answering.
    """
    return await asyncio.to_thread(_grab)
