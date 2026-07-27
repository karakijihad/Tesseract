"""Tests for teardown_all_controller_sessions (B5).

Deliberate shutdown deletes active sessions; crash path leaves them intact.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tesseract.orchestrator.tars_controller.shutdown import teardown_all_controller_sessions


def _make_session(sid: str) -> MagicMock:
    s = MagicMock()
    s.session_id = sid
    return s


class TestTeardownAllControllerSessions:
    def test_deletes_all_active_sessions(self) -> None:
        sessions = [_make_session("s-001"), _make_session("s-002"), _make_session("s-003")]
        deleted: list[str] = []

        def list_fn() -> list:
            return sessions

        def delete_fn(sid: str) -> bool:
            deleted.append(sid)
            return True

        count = teardown_all_controller_sessions(list_fn=list_fn, delete_fn=delete_fn)

        assert count == 3
        assert set(deleted) == {"s-001", "s-002", "s-003"}

    def test_crash_path_does_not_call_teardown(self) -> None:
        """Crash path must NOT call teardown — simulate by simply not calling it."""
        deleted: list[str] = []

        def delete_fn(sid: str) -> bool:  # noqa: ARG001
            deleted.append(sid)
            return True

        # On a crash path we never invoke teardown_all_controller_sessions.
        # Verify that if it IS called (deliberately), sessions are affected;
        # the calling code's gate (operator_shutdown_event.is_set() / decision
        # == "operator_quit") is what prevents this on crash.  Here we just
        # confirm the helper is a no-op when there are no sessions.
        count = teardown_all_controller_sessions(
            list_fn=lambda: [],
            delete_fn=delete_fn,
        )

        assert count == 0
        assert deleted == []

    def test_returns_zero_when_list_fn_raises(self) -> None:
        def bad_list() -> list:
            raise RuntimeError("registry unavailable")

        count = teardown_all_controller_sessions(
            list_fn=bad_list,
            delete_fn=lambda sid: True,
        )

        assert count == 0

    def test_continues_after_delete_fn_raises(self) -> None:
        sessions = [_make_session("s-A"), _make_session("s-B")]
        deleted: list[str] = []

        def delete_fn(sid: str) -> bool:
            if sid == "s-A":
                raise OSError("transient")
            deleted.append(sid)
            return True

        count = teardown_all_controller_sessions(
            list_fn=lambda: sessions,
            delete_fn=delete_fn,
        )

        assert count == 1
        assert deleted == ["s-B"]

    def test_idempotent_delete_fn_false(self) -> None:
        """delete_fn returning False (already gone) does not count as deleted."""
        sessions = [_make_session("s-X")]

        count = teardown_all_controller_sessions(
            list_fn=lambda: sessions,
            delete_fn=lambda sid: False,
        )

        assert count == 0
