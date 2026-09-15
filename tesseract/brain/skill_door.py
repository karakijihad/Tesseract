"""The one door a new skill is written through.

Every draft arrives here, whether the assistant wrote it (`skill_create`,
on its own judgement, when the operator asked, or on an observer's skill
nudge) or a task accepted done produced it (`playbook_extract`).
`create_skill` takes a drafted skill and the evidence behind it, runs the door (the procedure contract's blocking gaps and
the credential/path scan, `refuse_playbook`), dedupes against what already
exists, writes the draft to `workspace/skills/pending/`, files one
`skill_approval` card, and promotes it on the same rule wherever the draft
came from (`promotion_is_auto`).

**Dedupe is two questions, both asked before anything is written.** Is the
name already active, pending or rejected — refused, naming what is already
there. Does the proposed steps' tool sequence already match an existing
active or pending skill exactly — refused too, *except* for the
task-accepted-done origin, where a second success on the same steps is not a
duplicate proposal but supporting evidence for a skill that is already live,
recorded onto it (`brain.skills.add_evidence`) rather than written as a
second one. This is the behaviour `playbook_extract` already had and this
keeps.

**`origin` says where a draft came from** (`agent`, `operator`, `observer`,
`task_done`) and is recorded on the card payload. It also decides two things
inside the door: whether a tool-sequence match supports an existing skill
instead of refusing (`task_done` only), and whether the door may promote a
draft without the call itself being attended (`task_done` only — a scheduled
job is never attended, and that has always been how `playbook_extract`
promoted on its own; every other origin still needs `attended=True` AND
`skill_create`'s own posture resolving to auto, so an unattended tool call
cannot reach into the active tree on the strength of a mode it did not ask
about).

Five files, one responsibility each: this one orchestrates;
`skill_render.py` renders a draft and runs the gate; `skill_dedupe.py`
answers the tool-sequence question; `skill_card.py` files and settles the
proposal card; `skill_promotion.py` moves a draft once it exists. The names
are re-exported here so every caller reaches the lifecycle from one import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from tesseract.brain.skill_card import file_card, settle_card_approved
from tesseract.brain.skill_dedupe import (
    collapsed_tools,
    matching_sequence,
    scan_sequences,
)
from tesseract.brain.skill_promotion import (
    archive_rejected_skill,
    promote_pending_skill,
    promotion_is_auto,
)
from tesseract.brain.skill_render import (
    refuse_playbook,
    render_skill_markdown,
    validate_roundtrip,
)
from tesseract.brain.skills import (
    SKILL_FILENAME,
    SKILL_PENDING_DIRNAME,
    add_evidence,
    list_pending_skills,
    list_rejected_skills,
    list_skills_names,
    read_rejection_reason,
)
from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_skill_pending_cap,
)
from tesseract.orchestrator.background_event_bus import get_background_bus
from tesseract.workspace_events import EventStore

logger = logging.getLogger(__name__)

# Re-exported so `from tesseract.brain.skill_door import ...` reaches the
# whole lifecycle from one import path, whichever of the four files a name
# actually lives in.
__all__ = [
    "DraftStep", "SkillDraft", "CreateSkillResult", "Origin",
    "render_skill_markdown", "refuse_playbook", "create_skill",
    "scan_sequences", "matching_sequence",
    "promotion_is_auto", "promote_pending_skill", "archive_rejected_skill",
    "file_card", "settle_card_approved",
]

Origin = Literal["agent", "operator", "observer", "task_done"]


@dataclass(frozen=True)
class DraftStep:
    do: str
    tool: str = ""


@dataclass(frozen=True)
class SkillDraft:
    """Everything a new skill declares, before it is rendered to markdown.
    The runtime's own fields (`version`, `status`) are not here: a created
    skill is always version 1, active, and `render_skill_markdown` stamps
    both — never the caller's to set."""

    name: str
    description: str
    instructions: str
    rationale: str
    proposer: str = "entity"
    license: Optional[str] = None
    allowed_tools: tuple[str, ...] = ()
    trigger: str = ""
    use_when: str = ""
    not_when: str = ""
    preconditions: tuple[str, ...] = ()
    steps: tuple[DraftStep, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    expected_result: str = ""
    failure_modes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    confidence: Optional[float] = None


@dataclass(frozen=True)
class CreateSkillResult:
    """What the door decided. `status` starting with `refused_` means
    nothing was written (or, for `refused_card` alone, was written and then
    taken back); every other status left a folder on disk. `reason` carries
    the refusal sentence; `card_note` carries the card outcome for a
    successful write; `matched_name` is set only for `supported`."""

    status: Literal[
        "refused_duplicate_active",
        "refused_duplicate_pending",
        "refused_duplicate_rejected",
        "refused_duplicate_sequence",
        "refused_capped",
        "refused_roundtrip",
        "refused_gate",
        "refused_write",
        "refused_card",
        "supported",
        "created_pending",
        "created_active",
    ]
    name: str
    folder: Optional[Path] = None
    event_id: str = ""
    matched_name: str = ""
    reason: str = ""
    card_note: str = ""


async def create_skill(
    draft: SkillDraft,
    *,
    skills_dir: Path,
    origin: Origin,
    attended: bool,
    event_store: Optional[EventStore] = None,
    app_provider: Optional[Callable[[], Any]] = None,
    tool_names: Optional[frozenset[str]] = None,
    registry: Any = None,
    card_title: Optional[str] = None,
    card_summary: Optional[str] = None,
    card_extra: Optional[dict[str, Any]] = None,
) -> CreateSkillResult:
    """Write a new skill, wherever the draft came from.

    `attended` is whether the CALL is attended (a live tool call with an
    operator on the other end of `ask_fn`) — never true for a scheduled job.
    It gates two things: the unattended-drafts cap, and, together with
    `origin`, whether the door may promote without a hand.
    """
    name = draft.name

    if name in list_skills_names(skills_dir):
        return CreateSkillResult(
            status="refused_duplicate_active", name=name,
            reason=f"Skill {name!r} already exists in {skills_dir}.",
        )
    if name in list_pending_skills(skills_dir):
        return CreateSkillResult(
            status="refused_duplicate_pending", name=name,
            reason=(
                f"Skill {name!r} already pending promotion in "
                f"{skills_dir}/{SKILL_PENDING_DIRNAME}/. Decide its "
                "skill_approval card before proposing it again."
            ),
        )
    if name in list_rejected_skills(skills_dir):
        rejection = read_rejection_reason(skills_dir, name)
        return CreateSkillResult(
            status="refused_duplicate_rejected", name=name,
            reason=(
                f"Skill {name!r} was previously rejected by the operator"
                + (f": {rejection}" if rejection else ".")
                + " Address the rejection before re-proposing, or pick a different name."
            ),
        )

    sequence = collapsed_tools(draft.steps)
    if sequence:
        match = matching_sequence(scan_sequences(skills_dir), sequence)
        if match is not None:
            match_folder, match_name = match
            if origin == "task_done":
                err = add_evidence(match_folder, list(draft.evidence))
                if err is None:
                    return CreateSkillResult(
                        status="supported", name=name, folder=match_folder,
                        matched_name=match_name,
                    )
                return CreateSkillResult(status="refused_write", name=name, reason=err)
            return CreateSkillResult(
                status="refused_duplicate_sequence", name=name,
                reason=(
                    f"Refused: {match_name!r} already covers this same "
                    f"sequence of tools ({', '.join(sequence)}). Use or "
                    "refine that one instead of drafting a duplicate."
                ),
            )

    # Headless flood guard — UNATTENDED drafts are capped by
    # runtime.yaml::skill_pending_cap. Attended drafts went through the
    # operator's ASK and stay uncapped. `task_done` is a scheduled job, never
    # attended, and bounds itself a different way (`max_proposals` per pass,
    # in its own config) — the cap this guards against is a flood of DRAFT
    # calls with no bound of their own, which a job with a per-pass ceiling
    # already is not.
    if not attended and origin != "task_done":
        cap = load_skill_pending_cap(default_runtime_config_path())
        pending_now = len(list_pending_skills(skills_dir))
        if pending_now >= cap:
            return CreateSkillResult(
                status="refused_capped", name=name,
                reason=(
                    f"skills/pending/ already holds {pending_now} drafts "
                    f"(cap {cap}). Review the open proposal cards before "
                    "proposing more skills."
                ),
            )

    rendered = render_skill_markdown(draft)
    roundtrip_error = validate_roundtrip(rendered, name)
    if roundtrip_error:
        return CreateSkillResult(
            status="refused_roundtrip", name=name,
            reason=f"Rendered SKILL.md failed loader round-trip: {roundtrip_error}",
        )
    refused = refuse_playbook(rendered, name, tool_names)
    if refused:
        return CreateSkillResult(status="refused_gate", name=name, reason=refused)

    pending_dir = skills_dir / SKILL_PENDING_DIRNAME / name
    pending_dir.mkdir(parents=True, exist_ok=True)
    skill_path = pending_dir / SKILL_FILENAME
    try:
        _atomic_write(skill_path, rendered)
    except OSError as exc:
        return CreateSkillResult(
            status="refused_write", name=name, reason=f"Failed to write skill file: {exc}",
        )

    try:
        get_background_bus().publish(
            "SkillCreated", {"skill": name, "proposer": draft.proposer, "origin": origin},
        )
    except Exception:
        logger.warning("skill_door: bus publish failed for %s", name, exc_info=True)

    # `task_done` is the one origin nothing else would ever pick a draft back
    # up from: it is a background job, so a draft with no card is a draft
    # nobody is ever asked to look at. Every other origin is a live
    # conversation where the assistant can say so itself if the card fails.
    card_required = origin == "task_done"
    event_id, card_note, err = await file_card(
        event_store, app_provider, name, draft, rendered, origin,
        card_title=card_title, card_summary=card_summary, card_extra=card_extra,
        required=card_required,
    )
    if err is not None:
        _remove_draft(pending_dir)
        return CreateSkillResult(status="refused_card", name=name, reason=err)

    logger.info("Skill created (pending): %s (%s)", name, skill_path)

    may_promote_unattended = origin == "task_done"
    if (attended or may_promote_unattended) and promotion_is_auto(registry):
        entry, promote_err = promote_pending_skill(skills_dir, name)
        if promote_err is None and entry is not None:
            settle_card_approved(event_store, name, event_id)
            return CreateSkillResult(
                status="created_active", name=name, folder=skills_dir / name,
                event_id=event_id, card_note=card_note,
            )
        logger.warning("skill_door: auto promotion of %s failed: %s", name, promote_err)

    return CreateSkillResult(
        status="created_pending", name=name, folder=pending_dir,
        event_id=event_id, card_note=card_note,
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via a .tmp intermediate."""
    import os

    tmp_path = path.with_suffix(".md.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def _remove_draft(folder: Path) -> None:
    try:
        (folder / SKILL_FILENAME).unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        logger.warning("skill_door: could not remove the draft at %s", folder, exc_info=True)
