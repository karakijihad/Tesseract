"""Completion rates per agenda source and per worker lane.

Now that a run that produced nothing cannot reach ``DONE``, "completed" means
something and can be counted. Both figures are derived — the agenda's
append-only ``index.jsonl`` and the worker records on disk — so nothing has to
be kept in step, and a zero is reported as a zero rather than omitted.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Iterator

from tesseract.orchestrator.autonomy.paths import agenda_index_path
from tesseract.orchestrator.outcome import HEALTHY_OUTCOMES
from tesseract.orchestrator.workers.paths import workers_archive_dir
from tesseract.orchestrator.workers.record import list_active_records, load_record

log = logging.getLogger(__name__)

# An item in one of these has stopped waiting on the runtime. `blocked` counts
# as attempted-and-not-done: the work was dispatched and did not complete.
_CLOSED = frozenset({"done", "cancelled", "abandoned", "superseded", "blocked"})

# How many `YYYY-MM` archive buckets `lane_outcomes` reads. A year of history
# answers "what is this lane doing"; the buckets before it answer nothing that
# is still true, and reading them all would make the figure cost more every
# month it exists.
ARCHIVE_MONTHS = 12


@dataclass(frozen=True)
class SourceCompletion:
    source: str
    created: int
    open: int
    done: int
    blocked: int
    cancelled: int
    unattested: int
    """Of the `done` items, how many were closed by a worker that recorded no
    outcome. Every item closed before this phase is one of these: its `done`
    is exactly the claim the phase disproved, so it is reported rather than
    counted."""
    completion_rate: float | None
    """``attested done / attempted``. ``None`` when nothing from this source
    has finished yet — which is not the same as zero, and must not render as
    it."""


def _iter_index_rows() -> Iterator[dict[str, Any]]:
    path = agenda_index_path()
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                log.warning("autonomy: skipping malformed index.jsonl line %d", line_no)


_WORKER_REASON = re.compile(r"(?:worker_done|stale_repair_all_done):(wk-[A-Za-z0-9_\-]+)")


def _closing_worker(reason: str) -> str | None:
    """The worker id a `done` transition credits, if any. An item closed by
    the operator, by recovery, or by the bootstrap seed names none — there is
    no worker claim to attest, so nothing is doubted."""
    match = _WORKER_REASON.search(reason or "")
    return match.group(1) if match else None


def _is_attested(worker_id: str) -> bool:
    """Did the worker that closed this item record a healthy outcome?

    A record written before the outcome vocabulary carries none, and its
    `done` is precisely the claim this phase disproved — so it is reported
    apart from the rate rather than counted into it.
    """
    try:
        record = load_record(worker_id)
    except Exception:  # noqa: BLE001 — a bad record must not break the figure
        return False
    return record is not None and record.outcome in HEALTHY_OUTCOMES


def source_completion() -> list[SourceCompletion]:
    """One row per source that has ever produced an item, worst rate first."""
    source_of: dict[str, str] = {}
    last_status: dict[str, str] = {}
    closed_by: dict[str, str] = {}
    for row in _iter_index_rows():
        item_id = row.get("id")
        if not isinstance(item_id, str):
            continue
        if row.get("event") == "created":
            source_of[item_id] = str(row.get("source") or "unknown")
        to = row.get("to")
        if isinstance(to, str):
            last_status[item_id] = to
            if to == "done":
                worker_id = _closing_worker(str(row.get("reason") or ""))
                if worker_id:
                    closed_by[item_id] = worker_id
                else:
                    closed_by.pop(item_id, None)

    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "created": 0, "done": 0, "blocked": 0,
            "cancelled": 0, "open": 0, "unattested": 0,
        }
    )
    for item_id, source in source_of.items():
        bucket = counts[source]
        bucket["created"] += 1
        status = last_status.get(item_id, "")
        if status == "done":
            bucket["done"] += 1
            worker_id = closed_by.get(item_id)
            if worker_id is not None and not _is_attested(worker_id):
                bucket["unattested"] += 1
        elif status == "blocked":
            bucket["blocked"] += 1
        elif status in _CLOSED:
            bucket["cancelled"] += 1
        else:
            bucket["open"] += 1

    rows: list[SourceCompletion] = []
    for source, bucket in counts.items():
        attempted = bucket["done"] + bucket["blocked"] + bucket["cancelled"]
        attested = bucket["done"] - bucket["unattested"]
        rows.append(
            SourceCompletion(
                source=source,
                created=bucket["created"],
                open=bucket["open"],
                done=bucket["done"],
                blocked=bucket["blocked"],
                cancelled=bucket["cancelled"],
                unattested=bucket["unattested"],
                completion_rate=(attested / attempted) if attempted else None,
            )
        )
    rows.sort(key=lambda r: (r.completion_rate if r.completion_rate is not None else 2.0, r.source))
    return rows


def lane_outcomes(months: int = ARCHIVE_MONTHS) -> dict[str, dict[str, int]]:
    """``{worker kind: {outcome: count}}`` across active records and the most
    recent ``months`` archive buckets.

    The archive is append-only and grows for the life of the install, so the
    window is what keeps this a bounded read rather than one that gets slower
    every month it is asked.

    Records written before the outcome vocabulary existed count under
    ``unknown`` — a reader must not read their ``done`` status as success,
    because that is exactly the claim this phase disproved.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in list_active_records():
        counts[record.kind.value][
            record.outcome.value if record.outcome else "unknown"
        ] += 1
    archive = workers_archive_dir()
    if archive.exists():
        buckets = sorted(p for p in archive.iterdir() if p.is_dir())
        for month in buckets[-months:] if months > 0 else buckets:
            if not month.is_dir():
                continue
            for worker in sorted(month.iterdir()):
                path = worker / "record.json"
                if not path.exists():
                    continue
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                kind = raw.get("kind")
                if not isinstance(kind, str):
                    continue
                counts[kind][str(raw.get("outcome") or "unknown")] += 1
    return {kind: dict(outcomes) for kind, outcomes in counts.items()}


def completion_payload() -> dict[str, Any]:
    """Both views, JSON-ready, for the health surface."""
    return {
        "sources": [asdict(row) for row in source_completion()],
        "lanes": lane_outcomes(),
    }


__all__ = [
    "SourceCompletion",
    "completion_payload",
    "lane_outcomes",
    "source_completion",
]
