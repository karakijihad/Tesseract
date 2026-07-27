"""B1 — PTY consent was keyed on tab id instead of pane id.

Codex Finding #2 (2026-04-20). Frontend TerminalView sent `activeTabId`
(tab-*) as the consent key; backend pty_manager's gate checks actual
PTY pane ids (pane-*). Consent UI flipped but the forwarding gate
never opened.

Updated 2026-05-16 (terminal-control Phase 6): the per-pane consent
modal that owned this bug has been removed entirely. Observer is
always-on by default — the right-panel arm/disarm toggle is the
single operator control, and the backend auto-grants consent for
every live and future pane on arm (see
``PTYManager._maybe_auto_grant_consent`` /
``grant_consent_for_all_live`` and
``routes/observer_consent.arm()``). The B1 bug class can no longer
recur because there is no per-pane consent UI to mis-key. This test
now pins the *removal* — if a future refactor reintroduces the
modal, it must come back with the correct pane-id keying, but for
now the most we can verify is that the modal is gone.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[3]


def test_terminal_view_no_longer_renders_consent_prompt() -> None:
    tv = (REPO / "tesseract/mirror/src/views/TerminalView.tsx").read_text(encoding="utf-8")
    assert "<ConsentPrompt" not in tv, (
        "Phase 6 (terminal-control 2026-05-16): the per-pane consent "
        "modal was removed. If a future refactor reintroduces it, the "
        "B1 invariants (key on pane id, not tab id) must come with it."
    )
    assert "ConsentPrompt" not in tv, (
        "Stray import of ConsentPrompt — drop the unused symbol."
    )


def test_observer_store_dropped_ackconsent() -> None:
    """Phase 6 — observer-always-on. The frontend `ackConsent` path that
    used to fan out `observer_pane_ack` WS messages is gone; consent is
    auto-granted server-side on arm. Backend still parses
    `observer_pane_ack` messages defensively, but the frontend no
    longer originates them.
    """
    obs = (REPO / "tesseract/mirror/src/stores/observer.ts").read_text(encoding="utf-8")
    assert "ackConsent" not in obs, (
        "Phase 6: ackConsent removed from observer store; "
        "if reintroduced, restore the modal + B1 invariants together."
    )
    assert "observer_pane_ack" not in obs, (
        "Phase 6: frontend no longer originates observer_pane_ack."
    )
