"""``playbook_search`` — the door to a playbook this turn is not carrying.

The twin of ``tool_search``, and deliberately the same ergonomics. Every
playbook is named on the map in the prompt, with a line saying what it is;
the ones on the carried list (``brain/playbook_set.py``) arrive there with
their version, their status and their ``use_when`` as well. The rest cost one
call to get the same, plus the trigger, what must hold first, and where the
body is. A playbook off the list is slower to reach and never unavailable.

**This does not read the body, on purpose.** The result carries the contract
and the path; the assistant then reads ``SKILL.md`` with ``file_read``, which
is the act the usage log records (``brain/skill_usage.py``). Returning the
body here would answer the question and lose the measurement in the same call,
and the measurement is what says whether the carried list is set right.

A retired revision is not offered: it stays on disk as a record, and the
runtime has already judged it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.brain.playbook_contract import blocking_gaps
from tesseract.brain.playbook_set import CARRIED_FILENAME, load_carried_names
from tesseract.brain.skills import SkillEntry, load_skills
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult

#: Written out literally in `name` below as well: `sync_permissions` reads
#: the method statically and cannot follow a constant through a tool whose
#: constructor takes an argument. A test pins the two together.
PLAYBOOK_SEARCH_NAME = "playbook_search"

# The highest rank that counts as the query NAMING a playbook, on the same
# split `tool_search` draws: exact name, every term in the name, any term in
# the name. Rank 3 is a mention in the description or the trigger, which is
# the different question — "which of these did you mean".
_NAMED = 2


class PlaybookSearchInput(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "The playbook name you want, for example "
            "'run-a-career-scout-snapshot'. Plain words describing the job "
            "when you do not know the name."
        ),
    )


class PlaybookSearchTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "finding-a-playbook"
    summary: ClassVar[str] = "Read the whole of a playbook the turn is carrying only as a line."
    use_when: ClassVar[str] = (
        "A playbook marked (search) in your skills list looks right for this "
        "task. Name it and you get its trigger, what must hold first, its "
        "steps and its file, then read the body before following it. Plain "
        "words work when you do not know the name."
    )
    not_when: ClassVar[str] = (
        "a playbook already listed with its `use_when` is carried in full "
        "this turn. Read its `SKILL.md` with `file_read` and skip this. Use "
        "`tool_search` for a TOOL rather than a procedure."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir

    @property
    def name(self) -> str:
        return "playbook_search"

    @property
    def input_schema(self) -> type[BaseModel]:
        return PlaybookSearchInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, PlaybookSearchInput)
            else PlaybookSearchInput.model_validate(tool_input.model_dump())
        )
        terms = [t for t in inp.query.lower().split() if t]
        if not terms:
            return ToolResult(
                output=f"playbook_search({inp.query!r}): empty query", is_error=True
            )

        # Off the loop: one directory scan plus a YAML parse per playbook.
        entries = await asyncio.to_thread(load_skills, self._skills_dir)
        playbooks = [e for e in entries if e.is_playbook and e.status != "retired"]
        carried = load_carried_names(self._skills_dir / CARRIED_FILENAME)
        gaps = blocking_gaps(playbooks)

        query = inp.query.strip().lower()
        scored: list[tuple[int, SkillEntry]] = []
        for entry in playbooks:
            name = entry.name.lower()
            haystack = f"{entry.description} {entry.trigger} {entry.use_when}".lower()
            if name == query:
                rank = 0
            elif all(t in name for t in terms):
                rank = 1
            elif any(t in name for t in terms):
                rank = 2
            elif any(t in haystack for t in terms):
                rank = 3
            else:
                continue
            scored.append((rank, entry))
        scored.sort(key=lambda row: (row[0], row[1].name))

        # An exact name is not a search, it is a request. The five that merely
        # mention it would cost their contracts to answer a question that had
        # one answer.
        if scored and scored[0][0] == 0:
            scored = scored[:1]

        if not scored:
            return ToolResult(
                output=(
                    f"playbook_search({inp.query!r}): no playbook matched. Your "
                    "skills list names every one you have."
                )
            )

        blocks = [
            self._render(entry, rank, carried, gaps.get(entry.name))
            for rank, entry in scored
        ]
        named = sum(1 for rank, _ in scored if rank <= _NAMED)
        summary = f"playbook_search({inp.query!r}) matched {len(scored)} playbook(s)"
        if named < len(scored):
            summary += (
                f", {len(scored) - named} on their description only. Name the "
                "one you want to see the rest of it"
            )
        return ToolResult(output=summary + ":\n\n" + "\n\n".join(blocks))

    def _render(
        self, entry: SkillEntry, rank: int, carried: frozenset[str], gap
    ) -> str:
        """One playbook, as much of it as the rank earned.

        A description-only match gets the line it already has on the map plus
        the reason it surfaced. Naming it gets the contract: a match the
        assistant did not ask for by name is a candidate, and printing eight
        candidates in full is the cost this ranking exists to avoid.
        """
        head = f"`{entry.name}`: {entry.description}"
        if entry.name in carried:
            head += " Already carried this turn, with its `use_when`."
        if rank > _NAMED:
            return f"{head} Matched on what it says it is for."

        lines = [
            head,
            f"Playbook, v{entry.version or '?'}, {entry.status or 'no status'}.",
        ]
        if gap is not None:
            lines.append(f"Cannot run: {gap.field} {gap.detail}. Fix it before following it.")
        if entry.trigger:
            lines.append(f"Trigger: {entry.trigger}")
        if entry.use_when:
            lines.append(f"Use when: {entry.use_when}")
        if entry.not_when:
            lines.append(f"Not when: {entry.not_when}")
        for condition in entry.preconditions:
            lines.append(f"Needs first: {condition}")
        for index, step in enumerate(entry.steps, start=1):
            tool = f" (`{step.tool}`)" if step.tool else ""
            lines.append(f"{index}. {step.do}{tool}")
        if entry.expected_result:
            lines.append(f"Done looks like: {entry.expected_result}")
        for mode in entry.failure_modes:
            lines.append(f"Goes wrong: {mode}")
        # Relative, for the reason `prompt_content._build_skills_block` gives
        # at length: an absolute path resolves in an install and sends the
        # operator's home directory to a third-party provider with every
        # result. The read half of this pointer is a known open defect in a
        # packaged install and needs its own owner, not a wider allowlist.
        body = f"tesseract/workspace/skills/{entry.dirname}/SKILL.md"
        lines.append(f"Read the body before following it: `{body}`")
        return "\n".join(lines)


__all__ = ["PlaybookSearchTool", "PlaybookSearchInput", "PLAYBOOK_SEARCH_NAME"]
