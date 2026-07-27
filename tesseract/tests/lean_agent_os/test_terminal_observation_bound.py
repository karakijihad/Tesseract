"""Phase 5 Task 3 — proves the existing PTY-retention bound rather than
adding a new one (brief: "if a bound already exists, prove it with a
test instead of adding code").

`ObservationTranscript.pty_buffer` is a `deque(maxlen=PTY_LINE_CAP)` —
tail-bounded by line count. `PTY_LINE_MAX_CHARS` bounds a single line
(already covered by `fix_pass_2026_04_20/test_b2_b3_b4_b5_b6_contract.py
::test_b4_ansi_strip_and_line_cap`); this file covers the count-based
eviction that test doesn't exercise.
"""

from __future__ import annotations

from tesseract.brain.observation_transcript import (
    PTY_LINE_CAP,
    ObservationTranscript,
)


def test_pty_buffer_evicts_oldest_beyond_cap() -> None:
    t = ObservationTranscript()
    lines = [
        {"role": "pty", "pane_id": "p", "text": f"line-{i}\n", "timestamp": "t"}
        for i in range(PTY_LINE_CAP + 50)
    ]
    t.append_pty_lines(lines)

    assert len(t.pty_buffer) == PTY_LINE_CAP, (
        "pty_buffer grew past PTY_LINE_CAP — retention is unbounded, not tail-bounded"
    )
    # Oldest lines were evicted; only the most recent PTY_LINE_CAP survive.
    kept_texts = [line["text"] for line in t.pty_buffer]
    assert kept_texts[0] == "line-50\n"
    assert kept_texts[-1] == f"line-{PTY_LINE_CAP + 49}\n"
    assert "line-0\n" not in kept_texts


def test_pty_buffer_across_multiple_panes_still_bounded() -> None:
    """The cap is global across the transcript (not per-pane), but still
    bounds total retention regardless of how many panes feed it — a
    high-throughput multi-pane session cannot grow it unbounded."""
    t = ObservationTranscript()
    for pane_id in ("p1", "p2", "p3"):
        lines = [
            {"role": "pty", "pane_id": pane_id, "text": f"{pane_id}-{i}\n", "timestamp": "t"}
            for i in range(PTY_LINE_CAP)
        ]
        t.append_pty_lines(lines)

    assert len(t.pty_buffer) == PTY_LINE_CAP
