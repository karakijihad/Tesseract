"""lane-cleanup Batch 5 regression guards.

One real item (the rest of the audit's Batch-5 list was verified OVERREACH):

* #19 — `ClientMessage` Union and `_CLIENT_KINDS` dispatch dict must not drift.

P4 prune (2026-07-04): #22 (`_PtyLaneAdapter` close-grace / turn-timeout
config) was retired with the `pty` lane mode and `_PtyLaneAdapter` itself.
"""

from __future__ import annotations

import typing

from tesseract.orchestrator.tars_controller.protocol import (
    ClientMessage,
    _CLIENT_KINDS,
)


def test_client_message_union_matches_client_kinds() -> None:
    """#19 — every client message type must appear in BOTH the
    `ClientMessage` Union (used for typing) AND `_CLIENT_KINDS` (used by
    `parse_client_message` to dispatch). A type in one but not the other is
    a silent gap: an unreachable handler, or a handler the type checker
    can't see."""
    union_types = set(typing.get_args(ClientMessage))
    kinds_types = set(_CLIENT_KINDS.values())
    assert union_types == kinds_types, (
        "ClientMessage / _CLIENT_KINDS drifted — "
        f"only in Union: {union_types - kinds_types}; "
        f"only in dict: {kinds_types - union_types}"
    )
