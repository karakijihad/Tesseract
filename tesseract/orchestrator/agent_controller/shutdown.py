"""Deliberate-shutdown teardown for controller sessions.

Called ONLY from provably-deliberate-shutdown paths:
  - supervisor operator_quit branch
  - agent_controller.py run_controller when operator_shutdown_event is set

Never called from crash / respawn / heartbeat-failure paths so that B4
reattach can recover sessions after an unexpected restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


def teardown_all_controller_sessions(
    *,
    list_fn: Callable[[], list],
    delete_fn: Callable[[str], bool],
) -> int:
    """Delete every active controller session.

    Parameters
    ----------
    list_fn:
        Zero-argument callable returning a list of session records that have a
        ``session_id: str`` attribute.  Typically
        ``lambda: registry.list_sessions(status="active")``.
    delete_fn:
        Callable that accepts a session_id string and returns True if the
        session existed and was deleted.  Typically ``registry.delete_session``.

    Returns
    -------
    int
        Number of sessions deleted.
    """
    try:
        sessions = list_fn()
    except Exception:
        log.exception("teardown_all_controller_sessions: list_fn raised — skipping teardown")
        return 0

    deleted = 0
    for session in sessions:
        sid = session.session_id
        try:
            if delete_fn(sid):
                deleted += 1
                log.info("teardown: deleted controller session %s", sid)
            else:
                log.debug("teardown: session %s already gone", sid)
        except Exception:
            log.exception("teardown: delete_fn raised for session %s — continuing", sid)

    log.info("teardown_all_controller_sessions: deleted %d session(s)", deleted)
    return deleted
