"""P15-F: today+yesterday session-resume cutoff (frontend logic).

The frontend predicate `_isWithinResumeCutoff` lives in
`tesseract/mirror/src/stores/websocket.ts`. Source: `prompt.py`'s
`DAILY_FILES_TO_LOAD = 2` window — the chat-side memory system loads
today + yesterday's daily files, and the auto-resume cutoff must agree
so a fresh tab doesn't auto-resume a session whose memory context is
already gone.

This test is a contract pin on the backend constant. If `prompt.py`
changes the value, the frontend cutoff *must* be updated to match.
"""

from __future__ import annotations

from tesseract.brain import prompt


def test_daily_files_to_load_pins_resume_cutoff_at_two():
    """If this constant changes, update `_isWithinResumeCutoff` in
    `tesseract/mirror/src/stores/websocket.ts` and `isStale` in
    `tesseract/mirror/src/components/sessions/SessionDrawer.tsx`."""
    assert prompt.DAILY_FILES_TO_LOAD == 2, (
        f"DAILY_FILES_TO_LOAD changed to {prompt.DAILY_FILES_TO_LOAD}; "
        "frontend session-resume cutoff (websocket.ts + SessionDrawer.tsx) "
        "must be updated to match."
    )
