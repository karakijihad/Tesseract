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
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from tesseract.lib.log_envelope import BAD, INFO, SEVERITIES, WARN
from tesseract.orchestrator.narration import is_faithful as _is_faithful
from tesseract.orchestrator.watchman.findings import Finding, Sweep

log = logging.getLogger(__name__)

# How long the narration may be, PER FINDING it was given, plus a fixed
# allowance for the opening sentence.
#
# It was a flat 600 characters, and that flat number decided what the operator
# was told. Measured 2026-08-21 over a live 12-finding record: asked for one
# line per problem, `qwen2.5:7b` named every observed fault in all three
# samples and invented nothing in any of them — and two of the three were
# rejected anyway, at 1730 and 607 characters. The 3b passed the same gate
# every time by writing less and dropping four of the seven faults. A cap that
# selects for length when the operator needs content is selecting for the
# wrong thing.
#
# One line per finding means the bound is per finding: a report of twelve is
# longer than a report of two and should be.
NARRATION_CHARS_PER_FINDING = 160
NARRATION_CHARS_BASE = 320
# What no report may exceed however many findings there are. A record with
# forty faults needs a report a person will read, not a transcript.
MAX_NARRATION_CHARS = 2400
# How many of a finding's evidence lines ride into the model's input and into
# the summary body. A finding may carry ten; the first two are the reason and
# the rest are the log, which is what the evidence file and the pointer to it
# are for.
MAX_FACT_EVIDENCE = 2
_NUMBER = re.compile(r"\d+")

# An ISO timestamp is eight or more digits that mean a moment, not a count.
# `fact_lines` appends one to almost every line, and until they were taken out
# of the check a narration could claim `21 restarts` and pass because 21 was
# the day of the month. Stripped from BOTH sides so a narration may still
# quote a timestamp; that a quoted timestamp is one the facts actually carry
# is checked separately, below.
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?"
    r"(?:[+-]\d{2}:?\d{2}|Z)?"
)

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


def fact_lines(findings: Sequence[Finding]) -> list[str]:
    """One line per finding, plus the lines that say WHY, in the summary's words.

    Also the model's entire input. Anything it can say has to be derivable from
    this list, which is what makes the faithfulness check meaningful rather
    than decorative.

    Takes findings rather than a sweep because the caller decides WHICH set is
    being narrated, and that decision is the one this used to get wrong: a
    sweep has exactly one list in it, so the narration was always over every
    kept finding while the message it led counted only the ones a person can
    act on. A sentence about ten things above a header counting five is not a
    formatting fault; it is two audiences sharing one rendering.

    The evidence rides with the summary because without it the model was being
    asked to explain a fault from its count alone. `cli.codex.gpt56_terra …
    unavailable ×1` was all it ever saw, while `reason: binary not found on
    PATH` sat in the same finding and went only to a file on disk. A narration
    that cannot name a cause is not a weak narration; it is an uninformed one,
    and no wording of the prompt reaches that.

    Capped at `MAX_FACT_EVIDENCE` lines per finding rather than the ten a
    finding may carry: the model is being given the reason, not the log.
    """
    lines: list[str] = []
    for finding in findings:
        when = ""
        if finding.last_at is not None:
            when = f" (last at {finding.last_at.isoformat(timespec='seconds')})"
        lines.append(f"{finding.source}: {finding.summary_for_model}{when}")
        if not finding.quotable:
            continue
        for line in finding.evidence[:MAX_FACT_EVIDENCE]:
            lines.append(f"    quoted from {finding.source}: {line}")
    return lines


def narration_budget(facts: list[str]) -> int:
    """How long a narration over `facts` may be.

    Scales with what it was asked to describe, and is capped so a very bad
    night cannot produce a transcript.
    """
    findings = sum(1 for line in facts if not line.startswith("    quoted from"))
    return min(
        NARRATION_CHARS_BASE + findings * NARRATION_CHARS_PER_FINDING,
        MAX_NARRATION_CHARS,
    )


def is_faithful(narration: str, facts: list[str]) -> bool:
    """Every number in the narration appears in the facts it was given.

    Crude on purpose. A model asked to summarise counted facts fails in one
    characteristic way — inventing a plausible figure — and a check that
    catches that one reliably beats a judgement call that catches everything
    unreliably. Numbers absent from the facts mean the sentence is describing
    a runtime nobody observed.

    **The number check is the part that must never be relaxed.** It is the only
    thing that caught `gemma3:4b` reading the supervisor's `failures=3` — a
    soft limit — as the count of failures, and passing because the digit
    existed somewhere in the input. The LENGTH check beside it is a budget,
    not a correctness test, and it is the one that was throwing away correct
    reports.

    **Timestamps are taken out before the digits are counted.** `fact_lines`
    appends `(last at <iso>)` to nearly every line, and a date carries six or
    more numbers that mean a moment rather than a quantity. Left in, they
    licensed most small integers: a narration could say `21 restarts` over a
    record of two and pass, because 21 was the day of the month. They are
    stripped from the narration too, so quoting one is still allowed, and the
    quoted ones are checked against the facts as timestamps in their own
    right.

    The rule itself is `orchestrator/narration.py`'s, because the Autonomy
    panel writes its rooms' lines under the same one. The BUDGET stays here:
    it scales with the number of findings, which is this job's question.
    """
    return _is_faithful(narration, facts, budget=narration_budget(facts))


# ── how bad, at a glance ────────────────────────────────────────────

# One mark per severity, and the only place they are written down. A person
# reading a phone notification sees the first line and often nothing else, so
# the first thing in it says whether to open the message at all.
MARKERS = {BAD: "\U0001f534", WARN: "\U0001f7e0", INFO: "\U0001f7e2"}


def worst(findings: Sequence[Finding]) -> str:
    """The severity of the worst thing in this set.

    `info` when the set is empty, which is the honest answer: nothing is wrong
    is not the same claim as nothing was looked at, and the source table right
    below says which places were read.
    """
    return max((f.severity for f in findings), key=SEVERITIES.index, default=INFO)


def marker(severity: str) -> str:
    return MARKERS.get(severity, MARKERS[BAD])


# ── part 1, without a model ────────────────────────


def _plain_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def template_narration(findings: Sequence[Finding]) -> str:
    """The opening sentence, composed rather than generated.

    A model was treated as the only way to get a readable opening, and that
    assumption is what put a 4.7 GB download in front of the report working at
    all. It was true over a raw record, where the opening had to make sense of
    twelve unranked lines carrying six spellings of a timestamp. It is much
    less true over a record that is grouped, ranked and cause linked, because
    the structure now says most of what the sentence used to have to work out.

    So the template goes first and the model is measured against it. Measured
    live 2026-08-23 over six real incidents, `qwen2.5:7b` passed the
    faithfulness check in all three samples and all three RE-LISTED the
    bullets underneath: *"backend: agent-controller started 8 times in this
    window"* is the bullet, verbatim, one line above itself. A model given a
    list and asked to summarise it writes the list again, which is the exact
    duplication this message shape exists to stop.

    **What the opening has to add is the shape of the record, not its
    contents.** Two things a person cannot get from the bullets: how many of
    these are broken as opposed to merely degraded, and where the broken ones
    are. Both are now declared fields, so the sentence is a count and a
    placement, and it never restates a line the reader is about to see.

    An earlier version named every source and the largest repeat count. Over
    one incident that read well; over six from five sources it produced
    *"6 things need you, from backend, conscience, diagnostics,
    provider-health and schedule"*, which is a list pretending to be a
    sentence, and it led with a restart count because that was the biggest
    number rather than the worst thing.
    """
    if not findings:
        return ""
    broken = [f for f in findings if f.severity == BAD]
    degraded = [f for f in findings if f.severity == WARN]
    # Only what a person is being asked to act on is counted, never the whole
    # set handed in. The job passes `needs_you`, which holds nothing else, but
    # a sentence that says "3 things are broken" because an `info` finding was
    # in the list is the kind of wrong that survives review.
    count = len(broken) + len(degraded)
    if not count:
        return ""

    subject = "One thing is" if count == 1 else f"{count} things are"
    # Where the worst of it is. Naming every source turns the sentence into a
    # list; naming the places something is actually BROKEN is the one thing a
    # reader cannot work out from a count.
    where = _plain_list(sorted({f.source for f in (broken or degraded)}))

    if not broken:
        # Worth saying out loud rather than leaving to inference. An orange
        # message is still a message, and the first thing a person wants on
        # opening one is whether anything actually stopped working.
        return f"{subject} degraded, in {where}. Nothing is broken."
    if not degraded:
        return f"{subject} broken, in {where}."
    return (
        f"{count} things need you: {len(broken)} broken and "
        f"{len(degraded)} degraded. The broken ones are in {where}."
    )


# ── the summary ─────────────────────────────────────────────────────


def sections(sweep: Sweep, judgement: Any = None) -> list[tuple[str, tuple[Finding, ...]]]:
    """The findings, split into the lists each audience is shown.

    One list per audience, and the narration belongs to the first of them.
    Stage 5 already sorts what a person has to act on from what the runtime is
    saying about itself, and until now the report threw that away and printed
    one flat list — so the file described ten things under a heading, the
    message counted five, and neither said which five.

    A sweep with no judgement has no tiers to read, so it renders as one
    unranked list. That is the only shape a caller outside the job produces.

    Each list is grouped before it is shown, so four windows of one sleeping
    machine are one line here and one line on the phone. The judge's verdicts
    are not grouped: they are one per finding and the panel draws them that
    way.
    """
    from tesseract.orchestrator.watchman.grouping import group

    if judgement is None:
        return [("What happened", group(sweep.findings))] if sweep.findings else []
    out = [
        ("What needs you", group(judgement.needs_you)),
        ("Worth knowing", group(judgement.worth_knowing)),
    ]
    return [(title, found) for title, found in out if found]


def render_summary(sweep: Sweep, *, narration: str = "", judgement: Any = None) -> str:
    end = sweep.window_end
    start = sweep.window_start
    window = (
        f"{start.isoformat(timespec='seconds')} → {end.isoformat(timespec='seconds')}"
        if start else f"up to {end.isoformat(timespec='seconds')}"
    )
    kept = tuple(judgement.kept) if judgement is not None else sweep.findings
    lines = [
        f"# {marker(worst(kept))} What the runtime did, {end.astimezone().date().isoformat()}",
        "",
        f"Window: {window}",
        "",
    ]

    blocks = sections(sweep, judgement)
    # The sentence was written over what needs the operator. A record that
    # holds nothing of the kind cannot carry one, and rendering it beside the
    # only list left would be the same crossing this replaced.
    narration = narration if (judgement is None or judgement.needs_you) else ""
    if not blocks:
        lines += [
            "Nothing went wrong in this window. Every source below was read and "
            "had nothing to report.",
            "",
        ]
    for index, (title, found) in enumerate(blocks):
        lines += [f"## {title}", ""]
        # The sentence sits INSIDE the list it was written over, not above the
        # file. Where it floated at the top it read as a reading of everything
        # below it, which is exactly what it was not.
        if narration and index == 0:
            lines += [narration.strip(), ""]
        for finding in found:
            when = (
                f", last at {finding.last_at.isoformat(timespec='seconds')}"
                if finding.last_at else ""
            )
            lines.append(f"- **{finding.source}**: {md_safe(finding.summary)}{when}")
            # The reason, under the count. A count alone sends the operator
            # to a second file for the sentence that says what broke.
            for line in finding.evidence[:MAX_FACT_EVIDENCE]:
                lines.append(f"  - {md_safe(line)}")
        lines.append("")

    lines += ["## Where this was read", "",
              "| Source | Read | Rows |", "| --- | --- | --- |"]
    for read in sweep.reads:
        if read.error:
            state = f"could not be read: {read.error}"
        elif not read.present:
            state = "not on this machine"
        elif read.findings:
            state = (
                f"{len(read.findings)} finding"
                if len(read.findings) == 1
                else f"{len(read.findings)} findings"
            )
        else:
            state = "quiet"
        lines.append(f"| `{read.name}` | {state} | {read.scanned} |")
    lines.append("")

    if sweep.unread:
        lines += [
            "A source listed as *not on this machine* has no producer here yet. "
            "That is not the same as quiet, and this summary does not claim it is.",
            "",
        ]
    lines += _judgement_section(judgement)
    return "\n".join(lines).rstrip() + "\n"


def _judgement_section(judgement: Any) -> list[str]:
    """What the judge held back, and why, in the artifact itself.

    Not an appendix for completeness. The stages between collection and this
    file decide what the operator is told, and a decision nobody can read is
    indistinguishable from a bug that drops real faults. Printing it here puts
    the answer in the same file as the report, for a reader with no panel
    open.
    """
    dropped = getattr(judgement, "dropped", ()) if judgement is not None else ()
    blind = getattr(judgement, "blind_spots", ()) if judgement is not None else ()
    if not dropped and not blind:
        return []
    out: list[str] = []
    if dropped:
        out += [
            "## What was judged away",
            "",
            "These were in the window and are not in the report above. Each line "
            "says which stage decided, and what it read.",
            "",
        ]
        for verdict in dropped:
            out.append(
                f"- **{verdict.stage}**: {md_safe(verdict.finding.summary)}. "
                f"{md_safe(verdict.why)}"
            )
        out.append("")
    if blind:
        out += [
            "## What the judge could not see",
            "",
            "A rule with no evidence to read goes quiet the same way a rule "
            "with nothing to say does. These are the places that happened, so "
            "the report above may be carrying something a full record would "
            "have explained.",
            "",
        ]
        out += [f"- {md_safe(note)}" for note in blind]
        out.append("")
    return out


def write_summary(sweep: Sweep, *, narration: str = "", judgement: Any = None) -> Path:
    directory = watchman_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = sweep.window_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M")
    path = directory / f"{stamp}.md"
    path.write_text(
        render_summary(sweep, narration=narration, judgement=judgement), encoding="utf-8"
    )
    _write_latest(sweep, path, narration=narration, judgement=judgement)
    return path


def _write_latest(
    sweep: Sweep, summary_path: Path, *, narration: str, judgement: Any = None
) -> None:
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
                # WHAT it is about, and the surface needs it: a room that lists
                # a failing ref under the name of the log it was read from
                # tells the operator where to look rather than what is wrong.
                "subject": f.subject,
                "summary": f.summary,
                # The same finding as the operator's copy above, in the words
                # that may leave this machine. The panel's room lines are
                # written by a model over these, and until this key existed
                # the only summary a reader of this file had was the one
                # carrying whatever text the runtime was handed.
                "model_summary": f.summary_for_model,
                "count": f.count,
                "last_at": f.last_at.isoformat() if f.last_at else None,
                "severity": f.severity,
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
        # Every verdict, not only the surviving ones. This is what the
        # Autonomy panel renders as the pipeline: what was collected, what
        # each stage did with it, and the sentence it did it on.
        "judgement": _judgement_payload(judgement),
        # The panel draws the stages; a stage that could not read its evidence
        # is part of what it has to show, or the panel is as blind as the
        # report was.
        "blind_spots": list(getattr(judgement, "blind_spots", ()) or ()),
    }
    path = watchman_dir() / "latest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _judgement_payload(judgement: Any) -> list[dict[str, Any]]:
    verdicts = getattr(judgement, "verdicts", ()) if judgement is not None else ()
    return [
        {
            "stage": v.stage,
            "action": v.action,
            "why": v.why,
            "tier": v.tier,
            "source": v.finding.source,
            "kind": v.finding.kind,
            "subject": v.finding.subject,
            "summary": v.finding.summary,
        }
        for v in verdicts
    ]


# ── the evidence report ─────────────────────────────────────────────

#: How a filed defect stamps its own name. One constant because two things
#: depend on the shape now: `write_evidence` mints it, and the atlas reads it
#: back to place the record in a window. A reader that spelled the format out
#: for itself would be a second opinion about when a finding was.
#:
#: **It is not the record of when.** The name is minute resolution and cannot
#: be widened, because the width is the identity of every file already on
#: disk. `read_evidence` reads the precise moment out of the body and keeps
#: this as the fallback; the reason that matters is written there.
EVIDENCE_STAMP = "%Y-%m-%dT%H%M"

#: How many characters of the name the stamp occupies. Derived from the format
#: rather than written as 15, so widening one cannot leave the other slicing a
#: prefix that no longer parses.
EVIDENCE_STAMP_WIDTH = len(
    datetime(2026, 1, 1, tzinfo=timezone.utc).strftime(EVIDENCE_STAMP)
)

#: The line `render_evidence` writes the full moment on.
_OBSERVED_AT = "- observed at: "

#: The two lines that say what a defect IS, as `render_evidence` writes them.
#: They are the record's own answer to "what went wrong and where", which is
#: what a name has to say. Read here rather than parsed by whoever wants a
#: name, so the writer and the reader cannot disagree about the format.
_SOURCE_LINE = "- source: `"
_KIND_LINE = "- kind: `"


def evidence_dir() -> Path:
    """Where filed defects live. One definition, so a reader and the writer
    cannot disagree about the tree."""
    return watchman_dir() / "evidence"


def name_of_defect(kind: str, source: str, filed_at: datetime | None) -> str:
    """What to call a filed defect: what went wrong, where, and when.

    The summary is NOT the name, and that is the whole point of this function.
    A finding's summary is the log line it was found in, which on the live
    tree ran to a median of 98 characters and a maximum of 200 — so a defect
    with five links printed five paragraphs in the map's own "what it touches"
    list, and the record beside the map became unreadable before it said
    anything. The summary is still the first line of the record's body, byte
    for byte, which is where it belongs and where the panel already shows it.

    The date is part of the name because without it seventy of these are
    called "logged error in backend" and nothing tells them apart. It is the
    calendar day and no finer: a person picking one out of a list is choosing
    between days, and the exact moment is a field on the record.
    """
    what = (kind or "").replace("_", " ").strip() or "something"
    where = (source or "").replace("_", " ").strip()
    named = f"{what} in {where}" if where else what
    return f"{named}, {filed_at.date().isoformat()}" if filed_at else named


def read_evidence(path: Path) -> tuple[datetime | None, str]:
    """When a filed defect was observed, and the line it leads with.

    Both come off what `render_evidence` put there: the title is the `#`
    heading, which is the finding's summary, and the moment is the
    `- observed at:` line, which carries the full timestamp.

    **The body, not the file name, is the record of when.** The name stamps
    only the minute, and a reader that trusted it dated a file to the top of
    its minute — so a defect filed thirteen seconds after an atlas build
    appeared to predate it, and the next verification pass reported a node the
    live graph could not have held as drift. The name cannot simply be
    widened: its width is the identity of every file already on disk, and a
    parser expecting seconds would reject all of them at once.

    So the name stays the fallback, for a file written before this line
    existed, and it is deliberately the coarse answer rather than no answer. A
    name that carries no stamp at all yields `None`, and the atlas skips the
    record rather than placing it in a window it cannot prove it belongs to.
    """
    filed_at, title, _, _ = read_evidence_fields(path)
    return filed_at, title


def read_evidence_fields(path: Path) -> tuple[datetime | None, str, str, str]:
    """`(observed_at, summary, kind, source)` off one read of the file.

    Four facts and one pass, because the atlas needs all of them to name the
    record and opening the file twice to read two lines of the same header is
    a cost paid once per defect per nightly build.
    """
    filed_at = evidence_minute(path.stem)
    title = ""
    kind = ""
    source = ""
    try:
        with path.open(encoding="utf-8") as fh:
            first = fh.readline().strip()
            if first.startswith("# "):
                title = first[2:].strip()
            for line in fh:
                if line.startswith(_OBSERVED_AT):
                    filed_at = _refine(filed_at, _moment(line[len(_OBSERVED_AT):]))
                    continue
                if line.startswith(_KIND_LINE):
                    kind = line[len(_KIND_LINE):].strip().rstrip("`")
                    continue
                if line.startswith(_SOURCE_LINE):
                    source = line[len(_SOURCE_LINE):].strip().rstrip("`")
                    continue
                # The header block is the first few lines and ends at the
                # first section. Reading past it would be reading the log.
                if line.startswith("## "):
                    break
    except OSError:
        pass
    return filed_at, title, kind, source


def evidence_minute(stem: str) -> datetime | None:
    """The minute the file name carries, taken by the format's own width."""
    try:
        return datetime.strptime(
            stem[:EVIDENCE_STAMP_WIDTH], EVIDENCE_STAMP
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _refine(named: datetime | None, stated: datetime | None) -> datetime | None:
    """The body may say WHERE IN the minute the name claims, and no more.

    The name is the file's identity and the writer minted both, so the two
    always agree on a file this runtime wrote. Bounding it keeps that true of
    a file it did not: the body is read from a tree the runtime owns, but a
    reader that took any date it was handed would let a line somebody typed
    place a record anywhere in the graph's window. Refine, never contradict.
    """
    if stated is None:
        return named
    if named is None:
        return None
    return stated if named <= stated < named + timedelta(minutes=1) else named


def _moment(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
        f"{_OBSERVED_AT}{observed_at.isoformat()}",
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
    directory = evidence_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = observed_at.astimezone(timezone.utc).strftime(EVIDENCE_STAMP)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{finding.source}-{finding.kind}".lower()).strip("-")
    # Source and kind are not unique within a sweep — one window can carry
    # five distinct backend error classes. Without this the fifth report
    # overwrote the first four and the run claimed to have filed five.
    fingerprint = sha1(finding.summary.encode("utf-8")).hexdigest()[:8]
    path = directory / f"{stamp}-{slug}-{fingerprint}.md"
    path.write_text(render_evidence(finding, observed_at=observed_at), encoding="utf-8")
    return path


__all__ = [
    "EVIDENCE_STAMP",
    "EVIDENCE_STAMP_WIDTH",
    "MAX_NARRATION_CHARS",
    "NARRATION_CHARS_BASE",
    "NARRATION_CHARS_PER_FINDING",
    "narration_budget",
    "md_safe",
    "cursor_path",
    "fact_lines",
    "is_faithful",
    "read_cursor",
    "evidence_dir",
    "evidence_minute",
    "name_of_defect",
    "read_evidence",
    "read_evidence_fields",
    "render_evidence",
    "render_summary",
    "template_narration",
    "marker",
    "worst",
    "sections",
    "watchman_dir",
    "write_cursor",
    "write_evidence",
    "write_summary",
]
