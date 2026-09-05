"""Shared archive helper for scheduled-task outputs.

Anything a scheduled job delivers to the operator (job-search shortlists,
digests, watches) is also written to a retrievable markdown file under

    <TESSERACT_HOME>/memory-store/scheduled/<job_name>/<iso-date>.md

so both the assistant (via ``memory_get`` / file reads) and the operator (via
Obsidian — memory-store is Obsidian-compatible markdown) can find "what
we have" without scraping the chat. Mirrors how ``brief_render`` persists
under ``memory-store/daily/briefs/``.

The archive is best-effort and one-file-per-day (latest run that day
wins): a write failure must never block delivery, and ``TESSERACT_HOME``
is resolved at call time so test fixtures that ``monkeypatch.setenv`` it
land in their temp dir, not production state.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from tesseract.lib.clock import to_local
from tesseract.paths import TESSERACT_HOME

log = logging.getLogger(__name__)


def _safe_segment(value: str) -> str:
    """Collapse a job name to a path-safe folder segment."""
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "unknown"


def _yaml_str(value: str) -> str:
    """YAML single-quoted scalar (doubles embedded quotes) so a colon,
    newline, or other special char in a frontmatter value can't break
    Obsidian's property parser."""
    return "'" + str(value).replace("\n", " ").replace("'", "''") + "'"


def archive_run(
    job_name: str,
    body: str,
    when: datetime,
) -> Path | None:
    """Atomically write ``body`` to the per-day archive file. Returns the
    path on success, ``None`` on any failure (logged, never raised)."""
    try:
        root = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
        # A per-day file is a calendar day, so it is named on the
        # operator's clock. `generated_at` below stays the UTC instant:
        # the day is what a person looks up, the instant is what orders.
        iso_date = to_local(when).strftime("%Y-%m-%d")
        out_dir = root / "memory-store" / "scheduled" / _safe_segment(job_name)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{iso_date}.md"
        front = [
            "---",
            f"job: {_yaml_str(job_name)}",
            f"date: {iso_date}",
            f"generated_at: {when.isoformat()}",
        ]
        # No destination in here. The archive records what the run PRODUCED;
        # where it went is the routing table's answer and can differ between
        # the day this was written and the day it is read.
        front.append("---")
        content = "\n".join(front) + "\n\n" + (body or "").strip() + "\n"
        # Unique temp name so two same-day re-fires can't race on a shared
        # ``.tmp`` mid-rename (os.replace is not atomic on Windows NTFS).
        tmp = out_dir / f".{iso_date}.{uuid.uuid4().hex[:8]}.tmp"
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception as exc:  # noqa: BLE001 — archiving must not block delivery
        log.warning("archive_run failed for %s: %s", job_name, exc)
        return None


__all__ = ["archive_run"]
