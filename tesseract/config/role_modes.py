"""The two values a `mode` may take, defined once.

Every layer that writes one — the Settings route, the write gate's schema, the
Mirror UI, the voice-lane document mutations — reads the pair from here rather
than restating it, because a writer using a third spelling produces something
that reads as off and runs anyway. That was a live defect: Settings wrote
`disabled` while the loader honoured only `inactive`.

Its own module rather than a corner of `loader.py` because `voice/lane_config.py`
needs these constants and is documented as carrying no config-loading
dependency — first-run provisioning reaches it without importing the server.
`loader.py` re-exports them, so importing either place is correct.
"""

from __future__ import annotations

from typing import Final

ROLE_MODE_ACTIVE: Final[str] = "active"
ROLE_MODE_INACTIVE: Final[str] = "inactive"
ROLE_MODES: Final[frozenset[str]] = frozenset({ROLE_MODE_ACTIVE, ROLE_MODE_INACTIVE})

__all__ = ["ROLE_MODE_ACTIVE", "ROLE_MODE_INACTIVE", "ROLE_MODES"]
