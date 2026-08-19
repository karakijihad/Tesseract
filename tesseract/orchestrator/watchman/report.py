"""One artifact: what happened, and what broke.

The summary is written whether or not a model was reachable, and whether or not
anything happened — a quiet runtime says it was quiet. The counted body is
rendered from the findings by this module; a model, when there is one, adds a
lead paragraph and nothing else.

**The model may not add facts.** The heartbeat prompt's "stay literal" rule was
a request; here it is checked. A narration is kept only if every number in it
appears in the counted facts it was given, and dropped otherwise — a sentence
that invents a figure about the runtime's health is worse than no sentence,
because the whole point of this artifact is that the operator can trust it
without going and reading the logs themselves.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

from tesseract.orchestrator.watchman.findings import Finding, Sweep

log = logging.getLogger(__name__)

# A lead paragraph, not an essay. The counted body below it is the artifact.
MAX_NARRATION_CHARS = 600
_NUMBER = re.compile(r"\d+")

# Anything that would end a line or a cell, which is the only way text the
# runtime was GIVEN can become text the runtime appears to be SAYING.
_LINE_BREAKS = re.compile(r"[\r\n\v\f  ]+")


def md_safe(text: str, *, limit: int = 300) -> str:
    """One line of markdown that carries `text` as data rather than as markup.

    These artifacts quote strings the runtime did not choose — a job name and
    summary an operator typed, a log line a provider wrote — into files the
    assistant is pointed at and told to read as state. A newline ends the row
    and everything after it reads as the file's own voice; a `|` opens a column
    that was never there; a backtick closes the code span a name sits in. None
    of that is exotic input, and all of it is one substitution away from being
    inert.
    """
    flat = _LINE_BREAKS.sub(" ", str(text)).replace("|", "\\|").replace("`", "'")
    flat = " ".join(flat.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def watchman_dir() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / "autonomy" / "watchman"


def cursor_path() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / "autonomy" / "watchman-cursor.json"


def read_cursor() -> datetime | None:
    path = cursor_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = str(raw.get("covered_to") or "")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def write_cursor(covered_to: datetime) -> None:
    path = cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"covered_to": covered_to.isoformat()}), encoding="utf-8")
    tmp.replace(path)


# ── the counted facts ───────────────────────────────────────────────


def fact_lines(sweep: Sweep) -> list[str]:
    """One line per finding, in the words the summary uses.

    Also the model's entire input. Anything it can say has to be derivable from
    this list, which is what makes the faithfulness check meaningful rather
    than decorative.
    """
    lines: list[str] = []
    for finding in sweep.findings:
        when = ""
        if finding.last_at is not None:
            when = f" (last at {finding.last_at.isoformat(timespec='seconds')})"
        lines.append(f"{finding.source}: {finding.summary}{when}")
    return lines


def is_faithful(narration: str, facts: list[str]) -> bool:
    """Every number in the narration appears in the facts it was given.

    Crude on purpose. A model asked to summarise counted facts fails in one
    characteristic way — inventing a plausible figure — and a check that
    catches that one reliably beats a judgement call that catches everything
    unreliably. Numbers absent from the facts mean the sentence is describing
    a runtime nobody observed.
    """
    if not narration.strip():
        return False
    if len(narration) > MAX_NARRATION_CHARS:
        return False
    allowed = set(_NUMBER.findall(" ".join(facts)))
    return all(number in allowed for number in _NUMBER.findall(narration))


# ── the summary ─────────────────────────────────────────────────────


def render_summary(sweep: Sweep, *, narration: str = "") -> str:
    end = sweep.window_end
    start = sweep.window_start
    window = (
        f"{start.isoformat(timespec='seconds')} → {end.isoformat(timespec='seconds')}"
        if start else f"up to {end.isoformat(timespec='seconds')}"
    )
    lines = [
        f"# What the runtime did — {end.date().isoformat()}",
        "",
        f"Window: {window}",
        "",
    ]
    if narration:
        lines += [narration.strip(), ""]

    if not sweep.findings:
        lines += [
            "Nothing went wrong in this window. Every source below was read and "
            "had nothing to report.",
            "",
        ]
    else:
        lines += ["## What happened", ""]
        for finding in sweep.findings:
            when = (
                f" — last at {finding.last_at.isoformat(timespec='seconds')}"
                if finding.last_at else ""
            )
            lines.append(f"- **{finding.source}** — {md_safe(finding.summary)}{when}")
        lines.append("")

    lines += ["## Where this was read", "",
              "| Source | Read | Rows |", "| --- | --- | --- |"]
    for read in sweep.reads:
        if read.error:
            state = f"could not be read — {read.error}"
        elif not read.present:
            state = "not on this machine"
        elif read.findings:
            state = f"{len(read.findings)} finding(s)"
        else:
            state = "quiet"
        lines.append(f"| `{read.name}` | {state} | {read.scanned} |")
    lines.append("")

    if sweep.unread:
        lines += [
            "A source listed as *not on this machine* has no producer here yet — "
            "it is not the same as quiet, and this summary does not claim it is.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def write_summary(sweep: Sweep, *, narration: str = "") -> Path:
    directory = watchman_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = sweep.window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M")
    path = directory / f"{stamp}.md"
    path.write_text(render_summary(sweep, narration=narration), encoding="utf-8")
    _write_latest(sweep, path, narration=narration)
    return path


def _write_latest(sweep: Sweep, summary_path: Path, *, narration: str) -> None:
    """The machine-readable half, for the surface that renders this.

    Carries every source's presence, not only its findings: a view that cannot
    tell "no producer" from "nothing happened" is the defect the liveness
    contract exists to prevent, and it cannot tell them apart from a payload
    that only lists what was found.
    """
    payload = {
        "observed_at": sweep.window_end.isoformat(),
        "window_start": sweep.window_start.isoformat() if sweep.window_start else None,
        "summary_path": str(summary_path),
        "narrated": bool(narration),
        "finding_count": len(sweep.findings),
        "defect_count": len(sweep.defects),
        "findings": [
            {
                "source": f.source,
                "kind": f.kind,
                "summary": f.summary,
                "count": f.count,
                "last_at": f.last_at.isoformat() if f.last_at else None,
                "defect": f.defect,
            }
            for f in sweep.findings
        ],
        "sources": [
            {
                "name": r.name,
                "present": r.present,
                "scanned": r.scanned,
                "findings": len(r.findings),
                "error": r.error,
            }
            for r in sweep.reads
        ],
    }
    path = watchman_dir() / "latest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── the evidence report ─────────────────────────────────────────────


def render_evidence(finding: Finding, *, observed_at: datetime) -> str:
    """A defect, written so it can be handed to whoever owns the thing that
    broke: what failed, when, how often, the lines, and what it ran on."""
    import platform
    import sys

    from tesseract import __version__

    lines = [
        f"# {md_safe(finding.summary)}",
        "",
        f"- source: `{finding.source}`",
        f"- kind: `{finding.kind}`",
        f"- occurrences: {finding.count}",
        f"- first seen: {finding.first_at.isoformat() if finding.first_at else 'unknown'}",
        f"- last seen: {finding.last_at.isoformat() if finding.last_at else 'unknown'}",
        f"- observed at: {observed_at.isoformat()}",
        "",
        "## Versions",
        "",
        f"- TESSERACT {__version__}",
        f"- Python {sys.version.split()[0]} on {platform.system()} {platform.release()}",
        "",
        "## Lines",
        "",
        "```",
    ]
    lines += list(finding.evidence) or ["(the source carried no quotable line)"]
    lines += ["```", ""]
    return "\n".join(lines)


def write_evidence(finding: Finding, *, observed_at: datetime) -> Path:
    directory = watchman_dir() / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = observed_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{finding.source}-{finding.kind}".lower()).strip("-")
    # Source and kind are not unique within a sweep — one window can carry
    # five distinct backend error classes. Without this the fifth report
    # overwrote the first four and the run claimed to have filed five.
    fingerprint = sha1(finding.summary.encode("utf-8")).hexdigest()[:8]
    path = directory / f"{stamp}-{slug}-{fingerprint}.md"
    path.write_text(render_evidence(finding, observed_at=observed_at), encoding="utf-8")
    return path


__all__ = [
    "MAX_NARRATION_CHARS",
    "md_safe",
    "cursor_path",
    "fact_lines",
    "is_faithful",
    "read_cursor",
    "render_evidence",
    "render_summary",
    "watchman_dir",
    "write_cursor",
    "write_evidence",
    "write_summary",
]
