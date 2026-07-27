"""Content-diff-merge for KB files.

Per ``_shared/knowledge-base-layout.md``:

- **Frontmatter** is canonical — refresher OWNS it. On every refresh the
  scraped frontmatter overwrites whatever was on disk.
- **Body markdown** is operator-editable. A three-way merge between
  (a) the body at the last refresh's tail (snapshot under
  ``<subdir>/.last-refresh/<filename>``), (b) the current body on disk,
  and (c) the new body the refresher would generate.
- Non-conflicting merges land silently; conflicts return a
  :class:`MergeConflict` so the caller emits a ``kb_merge_conflict``
  workspace event and leaves the file alone.

The merge is paragraph-granular (blocks separated by blank lines). Per
plan §2e, we mirror the soul-update merge intent — small surface, no
sub-line diff3, no anchor tracking. Conflict = the same paragraph was
edited on both sides.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_FM_BOUND = "---"
_PARA_SPLIT = re.compile(r"(\n\s*\n)", re.MULTILINE)


@dataclass(frozen=True)
class MergeConflict:
    """Returned when operator and refresher edited the same paragraph."""

    file: str
    sections: tuple[str, ...]  # short paragraph previews that conflicted


@dataclass(frozen=True)
class MergeResult:
    """Successful merge outcome."""

    file: str
    bytes_before: int
    bytes_after: int
    changed: bool  # False when proposed == current (no-op)
    diff_summary: str


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into ``(frontmatter_dict, body)``.

    Tolerates files without a frontmatter block (returns ``({}, text)``).
    Frontmatter parse failures raise ``yaml.YAMLError`` — callers must
    decide whether to fail loud or fall back.
    """
    if not text.startswith(_FM_BOUND + "\n") and not text.startswith(_FM_BOUND + "\r\n"):
        return {}, text
    rest = text[len(_FM_BOUND) + 1 :]
    end = rest.find("\n" + _FM_BOUND + "\n")
    if end < 0:
        end = rest.find("\n" + _FM_BOUND + "\r\n")
    if end < 0:
        return {}, text
    fm_raw = rest[:end]
    body = rest[end + len(_FM_BOUND) + 2 :]
    if body.startswith("\n"):
        body = body[1:]
    data = yaml.safe_load(fm_raw) or {}
    if not isinstance(data, dict):
        data = {}
    return data, body


def _serialize_frontmatter(data: dict[str, Any]) -> str:
    if not data:
        return ""
    dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()
    return f"{_FM_BOUND}\n{dumped}\n{_FM_BOUND}\n\n"


def _split_paragraphs(text: str) -> list[str]:
    if not text:
        return []
    parts = _PARA_SPLIT.split(text)
    # Recombine each paragraph with the trailing separator so reassembly
    # is loss-less when none of the paragraphs change.
    paragraphs: list[str] = []
    buf = ""
    for chunk in parts:
        if _PARA_SPLIT.fullmatch(chunk):
            paragraphs.append(buf + chunk)
            buf = ""
        else:
            buf = chunk
    if buf:
        paragraphs.append(buf)
    return paragraphs


def _three_way_paragraph_merge(
    ancestor: str,
    current: str,
    proposed: str,
) -> tuple[str, list[str]]:
    """Merge ``current`` (operator edits since ``ancestor``) with
    ``proposed`` (refresher's new version of ``ancestor``).

    Strategy — paragraph-by-paragraph set comparison against the ancestor:

    - For each ancestor paragraph, determine whether each side **kept it
      verbatim**. If both sides kept → unchanged. If operator dropped AND
      refresher dropped → both removed, gone. If operator kept and
      refresher dropped → refresher's deletion stands (refresher owns
      structural drift). If operator dropped and refresher kept/edited
      → CONFLICT (operator's intent disagrees with refresher's keep).
    - Net-new paragraphs from each side: both kept (refresher's first,
      operator additions appended). A paragraph added independently by
      both sides only lands once.
    - Operator-deleted ancestor paragraphs are filtered out of the
      refresher body so the operator's deletion intent is honored when
      no conflict is flagged.

    Refresher OWNS structural canonicality — when only the refresher
    edits or drops content, that wins. The conflict surface fires only
    when operator intent and refresher intent disagree about the same
    ancestor paragraph.
    """
    if ancestor == current:
        return proposed, []
    if ancestor == proposed:
        return current, []

    a_paras = _split_paragraphs(ancestor)
    c_paras = _split_paragraphs(current)
    p_paras = _split_paragraphs(proposed)

    a_set = {p.strip() for p in a_paras if p.strip()}
    c_set = {p.strip() for p in c_paras if p.strip()}
    p_set = {p.strip() for p in p_paras if p.strip()}

    conflicts: list[str] = []

    # Operator-dropped ancestor paragraphs the refresher still asserts
    # (either kept verbatim or kept under a different form). Disagreement
    # — surface as a conflict and leave file untouched at the caller.
    operator_dropped_ancestor = [
        p for p in a_paras if p.strip() and p.strip() not in c_set
    ]
    for dropped in operator_dropped_ancestor:
        s = dropped.strip()
        if s in p_set:
            # Refresher kept verbatim — direct disagreement with the
            # operator's deletion. Flag.
            conflicts.append(_preview(dropped))
            continue
        # The ancestor paragraph is gone on both sides. If the refresher
        # also introduced a NEW paragraph that wasn't in the ancestor,
        # treat that as the refresher's edited replacement and flag a
        # conflict (operator wanted it gone; refresher proposes a
        # replacement). This catches the symmetric "operator deleted,
        # refresher edited" case the v1 implementation missed.
        refresher_replacements = [
            r for r in p_paras
            if r.strip() and r.strip() not in a_set and r.strip() not in c_set
        ]
        if refresher_replacements:
            conflicts.append(_preview(dropped))

    # Build the merged body. Start from the refresher's paragraphs, but
    # filter out any that the operator explicitly removed from the
    # ancestor (honors deletion intent when no conflict is flagged).
    # The conflict path already returns at the caller without writing,
    # so this filter only matters on the non-conflicting path.
    operator_removed_set = {
        p.strip() for p in a_paras
        if p.strip() and p.strip() not in c_set
    }
    merged: list[str] = []
    for p in p_paras:
        s = p.strip()
        if s and s in operator_removed_set and s in a_set:
            # operator deleted this ancestor paragraph — drop from merge
            continue
        merged.append(p)

    # Operator-added paragraphs (in current, not ancestor, not refresher)
    # land after the refresher body so the operator's additions survive
    # without disturbing refresher ordering.
    operator_added = [
        p for p in c_paras
        if p.strip() and p.strip() not in a_set and p.strip() not in p_set
    ]
    for p in operator_added:
        merged.append(p)

    out = "".join(merged)
    if out and not out.endswith("\n"):
        out += "\n"
    return out, conflicts


def _preview(paragraph: str, limit: int = 80) -> str:
    s = paragraph.strip().splitlines()[0] if paragraph.strip() else ""
    return s[:limit]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kb-", suffix=path.suffix or ".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_kb_file(
    target: Path,
    *,
    new_frontmatter: dict[str, Any],
    new_body: str,
    snapshot_dir: Path | None = None,
) -> MergeResult | MergeConflict:
    """Apply a refresher write to ``target``, preserving operator hand-edits.

    Frontmatter is overwritten unconditionally. Body is three-way merged
    against the last-refresh snapshot. On conflict, ``target`` is left
    unchanged and a :class:`MergeConflict` is returned.

    On success, the new body is written and the snapshot updated so the
    next refresh has a fresh ancestor.
    """
    target = Path(target)
    snap_dir = snapshot_dir if snapshot_dir is not None else target.parent / ".last-refresh"
    snap_path = snap_dir / target.name

    new_body = new_body if new_body.endswith("\n") else new_body + "\n"
    proposed_text = _serialize_frontmatter(new_frontmatter) + new_body

    if not target.exists():
        # First write — no ancestor, no merge.
        _atomic_write(target, proposed_text)
        snap_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(snap_path, new_body)
        return MergeResult(
            file=target.name,
            bytes_before=0,
            bytes_after=len(proposed_text.encode("utf-8")),
            changed=True,
            diff_summary="initial write",
        )

    current_raw = target.read_text(encoding="utf-8")
    _, current_body = split_frontmatter(current_raw)

    ancestor_body = ""
    if snap_path.exists():
        ancestor_body = snap_path.read_text(encoding="utf-8")

    merged_body, conflicts = _three_way_paragraph_merge(
        ancestor_body, current_body, new_body
    )
    if conflicts:
        return MergeConflict(file=target.name, sections=tuple(conflicts))

    merged_text = _serialize_frontmatter(new_frontmatter) + merged_body
    if merged_text == current_raw:
        # No-op write — still refresh the snapshot so the ancestor
        # tracks the latest scraped baseline.
        snap_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(snap_path, new_body)
        return MergeResult(
            file=target.name,
            bytes_before=len(current_raw.encode("utf-8")),
            bytes_after=len(current_raw.encode("utf-8")),
            changed=False,
            diff_summary="no changes",
        )

    _atomic_write(target, merged_text)
    snap_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(snap_path, new_body)
    return MergeResult(
        file=target.name,
        bytes_before=len(current_raw.encode("utf-8")),
        bytes_after=len(merged_text.encode("utf-8")),
        changed=True,
        diff_summary=_summarize_diff(current_raw, merged_text),
    )


def _summarize_diff(before: str, after: str) -> str:
    b_lines = before.splitlines()
    a_lines = after.splitlines()
    added = max(0, len(a_lines) - len(b_lines))
    removed = max(0, len(b_lines) - len(a_lines))
    if added and removed:
        return f"+{added} / -{removed} lines"
    if added:
        return f"+{added} lines"
    if removed:
        return f"-{removed} lines"
    # equal line count, content differs
    return "content updated"
