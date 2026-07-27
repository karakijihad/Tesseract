"""D1 + D2 — frontend observer sync on reconnect + toast-flag reset.

- D1 (Codex #7): WS reconnect did not call observer.syncFromBackend(),
  so the HUD could show `armed`/`observing` against a backend that had
  already disarmed itself on WS cleanup.
- D2 (Claude coder M-2): _observerUnavailableToastShown was set to
  true on first observer_unavailable and never reset — subsequent
  failures after recovery produced no toast.

TSX source-level invariant checks.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def test_d1_reconnect_syncs_observer() -> None:
    ws = (REPO / "tesseract/mirror/src/stores/websocket.ts").read_text(encoding="utf-8")
    # The onopen handler must now call syncFromBackend so the frontend
    # observer state matches whatever the backend reset to during the
    # connection gap.
    assert "useObserverStore" in ws, "BUG (D1): observer store import missing"
    assert "syncFromBackend()" in ws, (
        "BUG (D1): ws.onopen does not call observer.syncFromBackend() — "
        "frontend may show stale armed/observing against a disarmed backend"
    )


def test_d2_toast_flag_resets_on_success() -> None:
    dispatch = (REPO / "tesseract/mirror/src/stores/dispatch.ts").read_text(encoding="utf-8")
    # The flag reset must live inside the observer_result branch so a
    # successful run re-arms the one-shot toast for future failures.
    assert "_observerUnavailableToastShown = false" in dispatch, (
        "BUG (D2): toast flag never reset — post-recovery failures suppressed"
    )
    # Make sure the reset sits in the observer_result success branch
    # (not before or after).
    lines = dispatch.splitlines()
    in_observer_result = False
    found_reset = False
    for line in lines:
        stripped = line.strip()
        if "case 'observer_result':" in stripped:
            in_observer_result = True
        elif stripped.startswith("case "):
            in_observer_result = False
        if in_observer_result and "_observerUnavailableToastShown = false" in stripped:
            found_reset = True
            break
    assert found_reset, (
        "BUG (D2): reset isn't inside the observer_result handler"
    )
