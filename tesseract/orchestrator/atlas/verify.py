"""Rebuild the graph and check the one on disk still matches its inputs.

The rebuild happens in memory rather than in a scratch tree. The point of the
exercise is "do the inputs still imply the file", and a second file on disk
adds a place for the answer to go wrong without adding anything to the answer.

It runs after the build, on the same inputs, so a difference means one of two
things and both are worth knowing:

* **The builder is not deterministic.** Same inputs, different graph — the map
  cannot be reasoned about at all, and nothing else in the system would say so.
* **Something edited the atlas.** The derived layer is disposable; a hand edit
  in it means someone is treating it as primary, and the next build will throw
  their work away.

It cannot catch an input that changed since the build, and does not pretend
to: comparing a week-old atlas against today's corpus reports every ordinary
edit as drift, which is how a check becomes noise and then becomes ignored.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tesseract.orchestrator.atlas import diff, store
from tesseract.orchestrator.atlas.build import BUILDER_VERSION, derive
from tesseract.orchestrator.atlas.config import AtlasConfig, load_atlas_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VerifyReport:
    drift: diff.Drift
    live_nodes: int
    rebuilt_nodes: int
    stale_version: bool
    duration_ms: float

    @property
    def clean(self) -> bool:
        return self.drift.clean and not self.stale_version


def run_verify(
    *,
    memory_store: Any,
    vault_manager: Any,
    now: datetime | None = None,
    config: AtlasConfig | None = None,
    atlas_path=None,
) -> VerifyReport:
    started = time.monotonic()
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cfg = config or load_atlas_config()

    live = store.load(atlas_path)
    rebuilt, _memories, _pages = derive(
        memory_store=memory_store,
        vault_manager=vault_manager,
        now=moment,
        windows=cfg.review_after_days,
        version=BUILDER_VERSION,
        # No reuse: a hash carried over from the file being checked would let
        # a changed source pass its own verification.
        reuse={},
    )
    return VerifyReport(
        drift=diff.compare(live, rebuilt),
        live_nodes=len(live.nodes),
        rebuilt_nodes=len(rebuilt.nodes),
        stale_version=live.builder_version != BUILDER_VERSION,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )


__all__ = ["VerifyReport", "run_verify"]
