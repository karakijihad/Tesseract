"""What each playbook has actually done, in one reader.

The question behind a verdict: this thing is carried on every turn, has it
earned that. `playbook_reuse.py` owns the arithmetic; this owns the ROW, so
the cockpit panel and the tool that answers the same question from a channel
cannot come to differ about what a playbook's record is. That was the whole
finding behind ruling 23: what the operator can ask for at the desk they can
ask for from a phone, and the answer is one reader every surface calls, never
a channel-shaped digest.

**Every number here is a count with its sample beside it, and none of them is
a verdict.** At the volumes this machine produces, four observations against
four move a rate by 25 points, and a playbook is consulted on the hard tasks
and skipped on the easy ones, so nothing on disk holds the counterfactual. So
the rows are shown and the hand decides. `playbook_judge` is where the hand is
given.

**`turn_calls` and `turn_cost_usd` are the turn's, not the playbook's.** A turn
does the work the playbook describes and everything else the turn was for, and
no record separates them. Every surface that prints them says so.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from tesseract.brain.playbook_contract import version_number
from tesseract.brain.playbook_reuse import Reuse, TurnFact, measure_all, worse_than
from tesseract.brain.playbook_set import CARRIED_FILENAME, load_carried_names
from tesseract.brain.skills import load_skills

#: The window every surface opens on. One constant, because the tool carried
#: its own 14 and claimed in its own description that it matched the panel,
#: which opened on 30. The panel's is the one kept: it is the number the
#: operator has been reading, and the tool's claim is now true rather than
#: the panel's window being cut to make it so.
DEFAULT_WINDOW_DAYS = 30

TURN_SCOPE_NOTE = (
    "Calls and cost are the whole turn's, not the playbook's: a turn does the "
    "work the playbook describes and everything else it was for, and nothing "
    "on disk separates them."
)

#: Summed straight across revisions, because each of these counts a READ and
#: a read of v1 and a read of v2 are two reads. Everything that counts a TURN
#: is folded over the union of the turns instead: one turn that read both
#: revisions is one turn, which closed once and made its calls once, and
#: adding the revisions up would bill it and grade it twice.
_SUMS = ("loads", "unjoined", "corrections", "retries")


def records(skills_dir: Path, *, window_days: int) -> list[dict[str, Any]]:
    """One row per live playbook, most read first.

    Retired playbooks are left out: a retired revision is a record, not an
    offer, and showing it as an unused playbook reads as a demotion candidate
    for something already demoted. Everything else is here INCLUDING the ones
    with nothing, because a playbook carried on every turn and never once read
    is the row this exists to show, and a reader that returns only what the
    usage log holds cannot show one.
    """
    entries = [
        e for e in load_skills(skills_dir) if e.is_playbook and e.status != "retired"
    ]
    carried = load_carried_names(skills_dir / CARRIED_FILENAME)
    by_name = measure_all(
        [e.name for e in entries], window_days=window_days, with_cost=True
    )

    rows: list[dict[str, Any]] = []
    for entry in entries:
        measured = by_name.get(entry.name, {})
        # Numeric, not lexical: a version is a whole number and `1, 10, 2` is
        # what a string sort makes of a playbook that has been revised ten
        # times.
        order = sorted(measured, key=lambda v: version_number(v) or 0)
        reuses = [measured[version] for version in order]
        revisions = [r.as_json() for r in reuses]
        row: dict[str, Any] = {
            "playbook": entry.name,
            "description": entry.description,
            "version": entry.version,
            "status": entry.status,
            "carried": entry.name in carried,
            "revisions": revisions,
        }
        for key in _SUMS:
            row[key] = sum(int(r[key]) for r in revisions)
        row.update(_over_the_turns(reuses))
        row.update(_did_the_last_revision_help(reuses, entry.version))
        rows.append(row)

    # Most read first, then the ones with nothing, alphabetically. The zeroes
    # sort last and are still all here, which is the same shape the tool half
    # of this surface uses and for the same reason.
    rows.sort(key=lambda r: (-int(r["loads"]), str(r["playbook"])))
    return rows


_NO_COMPARISON: dict[str, Any] = {
    "previous_version": "", "comparison": "unknown", "previous_trouble": None,
}


def _did_the_last_revision_help(
    reuses: list[Reuse], live_version: str,
) -> dict[str, Any]:
    """The live revision against the one before it, in the same window.

    The question nothing answered: a playbook is rewritten because it kept
    being corrected, and then nobody ever asks whether the rewrite worked.
    The revision boundary IS the refinement, so this needs no join to how the
    new text came to exist, only the two records either side of it.

    **`live_version` is passed in and checked, because `reuses` is built from
    the versions that appear in the usage LOG.** A revision approved this
    morning has no rows yet, so the top of that list is the revision it
    replaced, and comparing it with ITS predecessor would print a confident
    verdict about two superseded versions under the heading of the current
    one. That is the same mistake, one layer up, as judging a playbook on
    text it no longer has.

    `improved` is None far more often than not, and that is the honest
    answer rather than a gap: `worse_than` refuses a comparison when either
    side has nothing graded, and a revision made this week has barely been
    read. It becomes True or False once both sides have a record.
    """
    if len(reuses) < 2 or reuses[-1].version != live_version:
        return dict(_NO_COMPARISON)
    live, before = reuses[-1], reuses[-2]
    worse = worse_than(live, before)
    # Four answers, not three, because `worse_than` is a strict comparison and
    # equal trouble returns False from it. Read as "not worse" that became
    # "better than", so a revision that changed nothing measurable was
    # reported as an improvement.
    if worse is None:
        comparison = "unknown"
    elif live.trouble == before.trouble:
        comparison = "same"
    else:
        comparison = "worse" if worse else "better"
    return {
        "previous_version": before.version,
        "previous_trouble": before.trouble,
        "comparison": comparison,
    }


def _over_the_turns(reuses: list[Reuse]) -> dict[str, Any]:
    """Everything that belongs to a TURN, over the distinct turns.

    One turn can read two revisions of the same playbook, and it is one turn:
    it closed once, made its calls once and cost what it cost once. Summing
    the revisions' own totals would grade it twice as well as bill it twice,
    and "6 of 4 worked" is the shape that mistake takes on the screen. This is
    why `Reuse` carries the turns rather than totals over them.

    Cost stays None if any turn's was, because a partial total presented as a
    total is the same lie as an unmeasured zero.
    """
    facts: dict[str, TurnFact] = {}
    for reuse in reuses:
        for fact in reuse.turn_facts:
            facts[fact.run_id] = fact
    costs = [fact.cost_usd for fact in facts.values()]
    outcomes = Counter(fact.outcome for fact in facts.values())
    return {
        "turns": len(facts),
        "turn_calls": sum(fact.calls for fact in facts.values()),
        "turn_cost_usd": (
            None
            if not costs or any(c is None for c in costs)
            else round(sum(c for c in costs if c is not None), 6)
        ),
        "succeeded": outcomes["succeeded"],
        "failed": outcomes["failed"],
        "ungraded": outcomes["ungraded"],
    }


def as_text(rows: list[dict[str, Any]], *, window_days: int) -> str:
    """The same rows as lines, for a surface that has no table.

    A channel gets the numbers, not a summary of them: a digest written for
    one surface is the second reader this module exists to prevent.
    """
    if not rows:
        return "No playbooks yet."
    out = [f"Playbooks over the last {window_days} days, most read first.", ""]
    for row in rows:
        head = f"{row['playbook']} v{row['version']}"
        if row["carried"]:
            head += " (carried every turn)"
        out.append(head)
        if not row["loads"]:
            out.append("  never read in this window")
        else:
            out.append(
                f"  read {_n(row['loads'], 'time')} in {_n(row['turns'], 'turn')}: "
                f"{row['succeeded']} closed well, {row['failed']} did not, "
                f"{row['ungraded']} closed on the assistant's own word, "
                f"{row['unjoined']} joined no closed turn"
            )
            out.append(
                f"  {_n(row['corrections'], 'correction')} after a read, "
                f"read again mid-turn {_n(row['retries'], 'time')}"
            )
            if row["previous_version"]:
                out.append(f"  {_against_the_last_one(row)}")
            cost = row["turn_cost_usd"]
            spent = "cost was not measured" if cost is None else f"cost ${cost:.4f}"
            out.append(f"  those turns made {row['turn_calls']} tool calls and {spent}")
    out += ["", TURN_SCOPE_NOTE]
    return "\n".join(out)


def _n(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


__all__ = ["DEFAULT_WINDOW_DAYS", "TURN_SCOPE_NOTE", "as_text", "records"]


def _against_the_last_one(row: dict[str, Any]) -> str:
    """Whether the rewrite that produced the live revision was worth making.

    Said in words rather than as two rates, because the two rates invite a
    reader to compare samples of four and eleven as if they were the same
    measurement. Undecided is stated, never rounded to "no change".
    """
    was = f"v{row['previous_version']}"
    return {
        "unknown": f"too early to say whether it is better than {was}",
        "same": f"measuring the same as {was}",
        "better": f"better than {was}",
        "worse": f"worse than {was}",
    }[row["comparison"]]
