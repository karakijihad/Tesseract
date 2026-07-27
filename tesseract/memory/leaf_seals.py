"""AU-16 S1 — seal artefacts produced by ``SealJob``.

A seal is the durable record SealJob writes when a buffer's leaves are
compressed into a single summary unit. S1 stores seals as standalone
JSON files at::

    <TESSERACT_HOME>/memory-store/leaves/seals/<seal_id>.json

with shape::

    {
        "seal_id": "seal_<8hex>",
        "source_slug": "channel-telegram-12345",
        "sealed_at": "<iso8601 UTC>",
        "leaf_ids": ["leaf_aaaa1111", ...],
        "leaf_count": 7,
        "summary_body": "...concatenated highlights...",
        "summary_title": "..."
    }

S2's trees consume these — ``source_tree`` reads the seal, derives a
markdown summary file under ``memory-store/trees/source/<slug>/``.
S1 ships the artefact shape only; the trees stay greenfield until S2.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field

from tesseract.memory.leaves import _resolve_home

log = logging.getLogger(__name__)


def seals_root() -> Path:
    return _resolve_home() / "memory-store" / "leaves" / "seals"


def seal_path(seal_id: str) -> Path:
    return seals_root() / f"{seal_id}.json"


def mint_seal_id() -> str:
    return f"seal_{secrets.token_hex(4)}"


class Seal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seal_id: str
    source_slug: str
    sealed_at: datetime
    leaf_ids: list[str]
    leaf_count: int = Field(ge=1)
    summary_title: str = Field(max_length=200)
    summary_body: str = Field(max_length=10000)


def write_seal(seal: Seal) -> Path:
    """Atomic write of a seal artefact. Returns the resulting path."""
    target = seal_path(seal.seal_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f"{target.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    )
    tmp.write_text(
        json.dumps(seal.model_dump(mode="json"), indent=2, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def read_seal(seal_id: str) -> Seal | None:
    path = seal_path(seal_id)
    if not path.exists():
        return None
    try:
        return Seal.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("malformed seal %s", path, exc_info=True)
        return None


def iter_seals() -> Iterator[Seal]:
    root = seals_root()
    if not root.exists():
        return
    for path in sorted(root.glob("seal_*.json")):
        try:
            yield Seal.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("malformed seal %s", path, exc_info=True)


def build_summary(leaves: list, *, now: datetime | None = None) -> tuple[str, str]:
    """Compose ``(title, body)`` from a batch of admitted leaves.

    S1 keeps this lexical — title is the most-importance leaf's title;
    body is a bulleted line per leaf. S2 may swap in a model-driven
    summariser, but the artefact shape stays the same so consumers
    don't have to branch.

    ``now`` is the caller's seal-time stamp (so the body header matches
    ``Seal.sealed_at`` exactly). Defaults to ``datetime.now(timezone.utc)``
    when called outside the SealJob context.
    """
    if not leaves:
        return ("", "")
    ranked = sorted(leaves, key=lambda lf: (-int(lf.importance), lf.id))
    primary = ranked[0]
    base_title = primary.title.strip() if primary.title else ""
    if not base_title:
        first_line = primary.body.splitlines()[0:1] if primary.body else []
        base_title = first_line[0].strip()[:80] if first_line else "(untitled)"
    title = f"Seal: {len(leaves)} × {base_title}"[:200]

    when = now or datetime.now(timezone.utc)
    lines: list[str] = [
        f"# {title}",
        "",
        f"_Sealed {when.astimezone(timezone.utc).isoformat()} — {len(leaves)} leaves._",
        "",
    ]
    for lf in ranked:
        head = lf.title or (lf.body.splitlines()[0] if lf.body else "")
        head = head.strip()[:120] or "(empty)"
        lines.append(f"- **{lf.id}** (importance {lf.importance}): {head}")
    body = "\n".join(lines)
    return (title, body[:10000])


__all__ = [
    "Seal",
    "build_summary",
    "iter_seals",
    "mint_seal_id",
    "read_seal",
    "seal_path",
    "seals_root",
    "write_seal",
]
