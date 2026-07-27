"""Terminal-side helpers for PTY-driven CLI agents.

Owns the end-of-turn detectors that watch a PTY byte stream for
boundary signals (Claude stream-json events, Codex prompt-regex + idle
tail). The pane lifecycle itself stays in
``tesseract/mirror/server/pty_manager.py``; this package is the
detector-state-machine half of the substrate (MO-9-4 → MO-9-6).
"""
