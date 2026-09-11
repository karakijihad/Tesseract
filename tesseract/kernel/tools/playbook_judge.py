"""playbook_judge tool — the operator keeps a playbook, or drops it.

The one thing that decides whether a playbook stays. Everything else that
could have decided it cannot:

**Activation by matching never fires.** `playbook_extract` promotes a draft
when a second task's collapsed tool-name sequence equals an existing
playbook's, exactly. Measured on this machine: the most repeated sequence
carrying a tool is one call long, so no two tasks ever match and every draft
that was not carried by hand stayed a draft. A tool name is also not an
action, so the rule fails in both directions: two unrelated jobs whose calls
are `file_read` then `bash` match, and two genuinely equivalent jobs differing
by one inspection call do not.

**Retirement by measurement cannot run.** `skill_refinement` retires a
revision that measured worse than the one before it, which needs a
predecessor under `history/`. Every playbook here is `v1` with none, so zero
retirements are possible at any usage volume. And at these counts no
statistical rule would be honest anyway: load counts run 1 to 5, four
observations against four move a rate by 25 points, and a playbook is
consulted on the hard tasks and skipped on the easy ones, so the records hold
no counterfactual.

So the verdict is a hand, and this is where it is given. It is a tool rather
than a button because a decision only the cockpit can take is a decision the
operator cannot take away from the desk.

**It moves the two things that actually change behaviour.** `status`, because
`retired` is filtered out of the prompt manifest, `playbook_search`, the atlas
and the panel; and `carried.txt`, which decides what arrives on every turn
with its `use_when` rather than a name and a line. `draft` gates nothing at
all today, so setting it to `active` is bookkeeping and is done for the sake
of the record, not for an effect.

**Provenance is the turn record, not a new field.** The call itself says who
decided and when, joined by run id, which is the same account AR-19 built for
every other act. Nothing new is written into the frontmatter.

Writes: `workspace/skills/<name>/SKILL.md` (one frontmatter line),
`workspace/skills/carried.txt`, and for a pending draft the folder move
`skill_promote` and the reject path already own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.brain.playbook_set import (
    CARRIED_FILENAME,
    load_carried_names,
    write_carried,
)
from tesseract.brain.skills import (
    SKILL_PENDING_DIRNAME,
    list_pending_skills,
    load_skill_folder,
    load_skills,
    set_skill_status,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.kernel.tools.skill_promote import (
    archive_rejected_skill,
    promote_pending_skill,
)

logger = logging.getLogger(__name__)


class PlaybookJudgeInput(BaseModel):
    name: str = Field(description="Slug of the playbook being judged.")
    verdict: Literal["keep", "drop"] = Field(
        description=(
            "keep: it earned its place. It becomes active and is carried on "
            "every turn. drop: it did not. It is retired and stops being "
            "read anywhere."
        )
    )
    why: str = Field(
        default="",
        description="The operator's reason, in their words. Kept beside a dropped draft.",
    )


class PlaybookJudgeTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "finding-a-playbook"
    summary: ClassVar[str] = "Keep a playbook or drop it, on the operator's word."
    use_when: ClassVar[str] = (
        "Use when the operator says a playbook is worth keeping or is not "
        "worth having. Keeping one activates it and carries it on every turn. "
        "Dropping one retires it, so nothing reads it again."
    )
    not_when: ClassVar[str] = (
        "to read a playbook, use `playbook_search`; to write one, use "
        "`skill_create`; to reword one that is staying, use `skill_refine`. "
        "Never call this on your own account: the point of it is that the "
        "verdict came from somebody other than whoever did the work."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir

    @property
    def name(self) -> str:
        return "playbook_judge"

    @property
    def input_schema(self) -> type[BaseModel]:
        return PlaybookJudgeInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        # `permissions.yaml` decides. A hardcoded ASK here would be read
        # before the policy and would never reach the file that is the
        # authority on what asks.
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, PlaybookJudgeInput)
            else PlaybookJudgeInput(**tool_input.model_dump())
        )
        name = inp.name.strip()

        # Resolved by ENUMERATION, never by joining the name onto a path and
        # asking whether the result exists. Both lists come from reading the
        # directory, so a name carrying `..` or a separator matches nothing
        # and is answered with what is actually here. Joining first would
        # have handed `set_skill_status` and the reject archive a folder
        # outside the skills tree. `promote_pending_skill` guards itself the
        # same way; the other two do not.
        was_pending = False
        # The entry knows its own folder, and a frontmatter name is not
        # always its dirname: `skill_create` enforces equality but a
        # hand-placed folder need not, and joining the NAME onto the tree
        # would then miss a playbook that is plainly there.
        live = {e.name: e.dirname for e in load_skills(self._skills_dir)}
        if name in live:
            folder = self._skills_dir / live[name]
        elif name in list_pending_skills(self._skills_dir):
            folder = self._skills_dir / SKILL_PENDING_DIRNAME / name
            was_pending = True
        else:
            return ToolResult(
                output=self._not_found(name),
                is_error=True,
                caller_error=True,
            )

        entry = load_skill_folder(folder)
        if entry is None:
            return ToolResult(
                output=(
                    f"{name} is there but its SKILL.md does not parse, so there "
                    "is nothing to judge yet. Fix the frontmatter first."
                ),
                is_error=True,
                caller_error=True,
            )
        if not entry.is_playbook:
            return ToolResult(
                output=(
                    f"{name} is a skill, not a playbook, so there is no procedure "
                    "here to judge. Use `skill_promote` to activate a drafted "
                    "skill, or `skill_refine` to change one."
                ),
                is_error=True,
                caller_error=True,
            )

        if inp.verdict == "drop":
            return self._drop(name, folder, was_pending, inp.why)
        return self._keep(name, folder, was_pending)

    def _keep(self, name: str, folder: Path, was_pending: bool) -> ToolResult:
        moved = ""
        if was_pending:
            _entry, err = promote_pending_skill(self._skills_dir, name)
            if err is not None:
                return ToolResult(output=err, is_error=True)
            folder = self._skills_dir / name
            moved = "It came out of the pending folder. "

        err = set_skill_status(folder, "active")
        if err is not None:
            # The move already happened, so an error naming only the failed
            # write would send the operator looking in the pending folder for
            # a draft that is no longer there.
            return ToolResult(output=f"{moved}{err}", is_error=True)

        changed, err = self._set_carried(name, carried=True)
        if err is not None:
            return ToolResult(output=f"{name} is active. {moved}{err}", is_error=True)
        logger.info("playbook kept: %s (carried=%s)", name, changed)
        carrying = (
            "It is carried on every turn now, with when to use it."
            if changed
            else "It was already carried, and stays carried."
        )
        return ToolResult(
            output=f"Kept {name}. {moved}{carrying}",
            receipt=Receipt(kind="record", id=name, locator=str(folder)),
        )

    def _drop(self, name: str, folder: Path, was_pending: bool, why: str) -> ToolResult:
        if was_pending:
            err = archive_rejected_skill(self._skills_dir, name, why or None)
            if err is not None:
                return ToolResult(output=err, is_error=True)
            _changed, carried_err = self._set_carried(name, carried=False)
            logger.info("playbook draft dropped: %s", name)
            tail = (
                f" {carried_err}"
                if carried_err is not None
                else " It moved to the rejected folder with the reason beside it."
            )
            return ToolResult(
                output=f"Dropped the draft {name}.{tail}",
                is_error=carried_err is not None,
                receipt=Receipt(kind="record", id=name, locator=str(folder)),
            )

        err = set_skill_status(folder, "retired")
        if err is not None:
            return ToolResult(output=err, is_error=True)
        # A retired name left on the dial is the prompt carrying a line that
        # `playbook_search` and the panel have both stopped answering to, so a
        # failure here is the half-state this tool exists to prevent and is
        # said out loud rather than logged.
        changed, carried_err = self._set_carried(name, carried=False)
        if carried_err is not None:
            return ToolResult(
                output=f"{name} is retired, and {carried_err}", is_error=True
            )
        logger.info("playbook dropped: %s (was carried=%s)", name, changed)
        dial = " It came off the carried list." if changed else ""
        return ToolResult(
            output=(
                f"Dropped {name}. It is retired, so the prompt, `playbook_search`, "
                f"the atlas and the panel all stop reading it.{dial} The file "
                "stays on disk."
            ),
            receipt=Receipt(kind="record", id=name, locator=str(folder)),
        )

    def _set_carried(self, name: str, *, carried: bool) -> tuple[bool, str | None]:
        """Put the name on the dial or take it off.

        Returns whether the file changed, and an error to say out loud. The
        two answers are separate because collapsing them into one boolean
        made a failed write report as "it was already carried", which is a
        sentence about a state that is not the one on disk. The cockpit's own
        path (`routes/workspace.py::_commit_working_set_proposal`) says the
        same thing about the same two files.

        Written only when something actually comes on or off: rewriting it to
        identical bytes moves its mtime, which is what
        `generate_playbook_set --check` reads as a hand-edit.
        """
        path = self._skills_dir / CARRIED_FILENAME
        kept = set(load_carried_names(path))
        if carried == (name in kept):
            return False, None
        if carried:
            kept.add(name)
        else:
            kept.discard(name)
        try:
            write_carried(sorted(kept), path)
        except OSError as exc:
            logger.warning("playbook_judge: could not write %s", path, exc_info=True)
            verb = "onto" if carried else "off"
            return False, (
                f"the carried list could not be written ({exc}), so {name} is "
                f"still not {verb} it. Fix {path} and say the verdict again."
            )
        return True, None

    def _not_found(self, name: str) -> str:
        have = sorted(e.name for e in load_skills(self._skills_dir) if e.is_playbook)
        return (
            f"No playbook called {name!r}, active or pending. "
            f"Playbooks here: {', '.join(have) or '(none)'}"
        )
