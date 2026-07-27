"""B2 + B3 + B4 + B5 + B6 — observer contract-surface hardening.

- B2 (simplifier #1 / redundancy 2.3 / Codex #5): `/observe` WS path
  fired observe() AND observe_incremental() — double cost, double
  ingestion. Remove the incremental call from _cmd_observe.
- B3 (simplifier #6 / Codex #5): `mode` parameter silently ignored.
  Only `meta` is implemented today; `maintenance` is a deferred feature.
  Log warning + document; keep the parameter so REPL / routes still work.
- B4 (Claude pr-review SEC-1): Raw PTY output (ANSI escapes, possible
  secrets) inserted verbatim into observer system prompt.
  `append_pty_lines` now strips ANSI CSI sequences and caps line length.
- B5 (Claude pr-review SEC-2): `/api/observer/snapshot` accepted
  unbounded `state_payload`. Cap to 2 KiB serialized.
- B6 (Claude pr-review SEC-4): `grant_consent` didn't validate pane_id
  is a live pane. Add check.
"""

from __future__ import annotations

import inspect

from tesseract.brain.observation_transcript import ObservationTranscript


def test_b2_cmd_observe_no_incremental_call() -> None:
    from tesseract.mirror.server import commands
    src = inspect.getsource(commands.cmd_observe)
    # The old double-fire bug was calling observe_incremental() right
    # after observe(). That call must be gone.
    assert "observe_incremental" not in src, (
        "BUG (B2): /observe WS path still calls observe_incremental() — "
        "doubles cost and races with ObserverSubscriber loop_end."
    )


def test_b3_mode_documented() -> None:
    from tesseract.brain import observer
    src = inspect.getsource(observer)
    # One of these markers must appear so a reader sees maintenance ≠ meta.
    assert (
        "maintenance" in src.lower() and ("deferred" in src.lower() or "warn" in src.lower() or "not implemented" in src.lower())
    ), "BUG (B3): observer.py no longer calls out that `maintenance` mode is unimplemented"


def test_b4_ansi_strip_and_line_cap() -> None:
    t = ObservationTranscript()
    # Classic ANSI CSI sequences: bold + color + reset.
    raw = "\x1b[1;31mERROR\x1b[0m /etc/secret=sk-abcd\n"
    added = t.append_pty_lines([{"role": "pty", "pane_id": "p", "text": raw, "timestamp": "t"}])
    assert added == 1
    stored = t.pty_buffer[0]["text"]
    assert "\x1b[" not in stored, (
        f"BUG (B4): ANSI escape not stripped, text={stored!r}"
    )
    assert "ERROR" in stored and "secret=sk-abcd" in stored
    # Long line must be capped.
    huge = "x" * 10_000
    t.append_pty_lines([{"role": "pty", "pane_id": "p", "text": huge, "timestamp": "t"}])
    stored_huge = t.pty_buffer[-1]["text"]
    assert len(stored_huge) <= 2048, (
        f"BUG (B4): long PTY line not capped, len={len(stored_huge)}"
    )



def test_b6_grant_consent_validates_pane() -> None:
    from tesseract.mirror.server.pty_manager import PTYManager
    src = inspect.getsource(PTYManager.grant_consent)
    # After fix: grant_consent() must not blindly add pane_id when no
    # PTY is live with that id.
    assert "_ptys" in src or "isinstance" in src, (
        f"BUG (B6): grant_consent doesn't check pane_id is a live pane — {src}"
    )
