"""Autonomy digest — the agenda + failures cross-feed section of the
assistant's system prompt.

Split out of `tesseract/brain/prompt.py` for module size.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from tesseract.brain.autonomy_digest import (
    AgendaEntry,
    FailuresSnapshot,
    load_autonomy_digest_config,
    render_digest,
)
from tesseract.brain.prompt_content import _section

# Logger name pinned to "tesseract.brain.prompt" — see prompt_time.py's
# module docstring for why this is hardcoded rather than `__name__`.
logger = logging.getLogger("tesseract.brain.prompt")

# Open agenda statuses that count as "on the assistant's plate" — everything
# non-terminal (`TERMINAL_STATUSES` in `orchestrator/autonomy/models.py`).
# `unvetted` is not here and no longer needs to be: the vetter that held items
# in it is deleted, so nothing new can enter that status.
OPEN_AGENDA_STATUSES = frozenset({
    "proposed", "selected", "running", "awaiting_operator",
    "resume_queued", "blocked",
})

# Fix A2 (lean-agent-os P1 follow-up, Q4) — a bare "# Autonomy digest"
# header didn't read as the assistant's own commitments in a live test ("what's
# on your plate?" ignored it). This one-line lead makes the ownership
# explicit without touching the per-item line format.
AUTONOMY_DIGEST_LEAD = (
    "These are your own open commitments — treat them as part of what you are "
    "currently working on."
)


def _ranked_agenda_reader() -> Any:
    """Zero-arg getter over one memoized ``AgendaStore().ranked()`` call.

    One reader consumes it today; the memoization stays because
    ``render_digest`` isolates each reader behind its own try/except, so a
    second reader over the same ranked list is one line away and would
    otherwise silently double the scan.
    """
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    cache: list[Any] | None = None

    def get() -> list[Any]:
        nonlocal cache
        if cache is None:
            cache = AgendaStore().ranked()
        return cache

    return get


def _read_agenda_entries(ranked_items: list[Any]) -> list[AgendaEntry]:
    """Open (non-terminal, non-unvetted) agenda items, ranked by priority.

    ``ranked_items`` comes from ``AgendaStore().ranked()`` — the same
    accessor ``mirror/server/routes/agenda.py`` reads from — instead of
    parsing ``agenda/active/*.json`` ad hoc. ``AgendaStore.iter_active``
    already skips malformed records with a warning, so a corrupt file here
    never raises past this function.
    """
    return [
        AgendaEntry(title=item.goal, status=item.status.value, created_at=item.created_at)
        for item in ranked_items
        if item.status.value in OPEN_AGENDA_STATUSES
    ]


def _read_failures_snapshot(failures_scope: str | None) -> FailuresSnapshot:
    """P6 Task 3 §G4 (extended P6 Task 5) — tripped circuit breakers +
    cumulative-since-boot stall/vanished-spawn counts, plus the last
    consecutive same-tool error streak (`failures_signal`) for
    ``failures_scope`` (a `ChatSession._failures_scope_id`).

    ``failures_scope is None`` renders no streak line at all — correct for
    a frozen/boot prompt assembled with no per-turn session in flight
    (whole-phase review fix, 2026-07-06: the streak used to be a single
    process-global slot read by every caller regardless of which chat was
    running, so one chat's tool failure rendered in every other chat's
    digest).

    Breaker log dir resolved at call time (never cached) via the canonical
    `TESSERACT_HOME`-env-override idiom (`kernel/workspace_changes.py::
    workspace_events_dir`) so a test's `monkeypatch.setenv` lands the read
    under its own tmp dir.
    """
    import os

    from tesseract.brain import failures_signal
    from tesseract.context.circuit_breaker import load_tripped_breakers
    from tesseract.paths import TESSERACT_HOME, log_dir

    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    tripped = load_tripped_breakers(log_dir("circuit-breakers"))
    return FailuresSnapshot(
        tripped_breakers=tuple(sorted(tripped)),
        stalled_count=failures_signal.stalled_count(),
        vanished_count=failures_signal.vanished_count(),
        tool_error_streak=(
            failures_signal.tool_error_streak(failures_scope)
            if failures_scope is not None else None
        ),
    )


def _build_autonomy_digest_section(
    memory_store_dir: Path, failures_scope: str | None = None,
) -> str:
    """Render the agenda + failures cross-feed digest.

    Adjacent to the "Right now" block (rendered immediately before it) so
    background thinking reaches every turn without a tool call. Empty on
    all sources returns "" — no section, zero tokens.
    """
    get_ranked_agenda = _ranked_agenda_reader()
    digest_config = load_autonomy_digest_config()
    digest = render_digest(
        lambda: _read_agenda_entries(get_ranked_agenda()),
        failures_reader=lambda: _read_failures_snapshot(failures_scope),
        max_age_days=digest_config.max_age_days,
    )
    if not digest:
        return ""
    return _section("Autonomy digest", f"{AUTONOMY_DIGEST_LEAD}\n\n{digest}")
