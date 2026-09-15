"""skill_refine tool — the one way a skill changes.

Four actions, one tool, and each writes through a single door:

- ``report``  — the working agent's own account that a skill it just followed
  went wrong. Writes the correction row (`brain/skill_usage.py::
  log_correction`) and spawns the `skill-fixer` agent in the background,
  briefed with nothing but pointers: the skill's folder, the revision and
  step reported, and where the turn record is. The fixer reads the record
  itself rather than trusting the working agent's account of its own
  failure, which cannot tell a wrong procedure from one that was ignored.
- ``revise``  — files a pending `skill_refinement` card carrying a proposed
  SKILL.md and the sha256 of the live file it was written against. The
  watcher applies it once the card's posture is `auto`; the operator
  applies it by hand otherwise. This tool never writes the live file
  itself.
- ``revert``  — files the same kind of card, with an earlier kept revision
  from `history/<version>/` as the proposal. Applied, `replace_skill_body`
  re-stamps it as a new version, so the bad revision stays archived rather
  than being un-made.
- ``retire``  — files a `skill_retirement` card. It asks; it never decides.
  Only the operator's answer retires a skill, in every mode
  (`permissions.yaml::workspace_cards`).

**The gate is the approval**, same as it always was: a card's posture is
whatever `permissions.yaml` says for the mode the install runs in, and
`check_permissions` returns PASSTHROUGH so the policy is read before this
tool ever runs.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.brain.skill_usage import log_correction
from tesseract.brain.skills import (
    SKILL_FILENAME,
    SKILL_HISTORY_DIRNAME,
    list_history,
    load_skill_folder,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)


class SkillRefineInput(BaseModel):
    action: Literal["report", "revise", "revert", "retire"] = Field(
        description=(
            "report: a skill you just followed went wrong (a step failed, "
            "was wrong, or was missing); writes the record and hands it to a "
            "reviser. revise: you have read the live skill and it is wrong "
            "or incomplete; replace it with a checked rewrite, naming the "
            "sha256 of the exact file you read as base_sha256 (files a "
            "card; called by the fixer, not by the working agent). revert: "
            "restore an earlier kept revision as the live one (files a "
            "card). retire: ask the operator whether a skill should stop "
            "being used; only their answer decides, never this tool."
        )
    )
    name: str = Field(description="Slug of the skill.")
    version: str = Field(
        default="",
        description=(
            "report only: the revision you read, if you know it. Defaults "
            "to the skill's live version."
        ),
    )
    step: int | None = Field(
        default=None,
        description="report only: the step number where it went wrong, if you know it.",
    )
    proposed_markdown: str = Field(
        default="",
        description=(
            "revise only: the full revised SKILL.md (frontmatter + body). A step "
            "that calls no tool leaves out its `tool:` line."
        ),
    )
    rationale: str = Field(
        default="",
        description="revise/retire only: why, in a sentence or two. Recorded on the card.",
    )
    base_sha256: str = Field(
        default="",
        description=(
            "revise only, required: sha256 of the live SKILL.md you read "
            "before writing proposed_markdown. The card is refused if the "
            "live file has moved on by the time it is applied."
        ),
    )
    to_version: str = Field(
        default="",
        description=(
            "revert only: which kept revision to restore. Defaults to the "
            "newest one under history/."
        ),
    )


class SkillRefineTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "extending-yourself"
    summary: ClassVar[str] = "Report a skill that went wrong, revise, revert, or retire one."
    use_when: ClassVar[str] = (
        "Use `report` only when a skill you just followed went wrong: a step "
        "failed, was wrong, or was missing. Never call it on a clean use. "
        "Use `revise` to file a checked rewrite of a skill you have read and "
        "diagnosed (this is what the spawned fixer calls, not the working "
        "agent). Use `revert` to restore an earlier kept revision. Use "
        "`retire` to ask the operator whether a skill should stop being "
        "used."
    )
    not_when: ClassVar[str] = (
        "to read a skill, use `file_read`; to find one, use `playbook_search`; "
        "to create a new one, use `skill_create`. None of the four actions "
        "here apply themselves without the operator's policy allowing it, "
        "and `retire` never decides on its own account."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "queryable"

    def __init__(
        self,
        skills_dir: Path,
        event_store: Optional[EventStore] = None,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._event_store = event_store
        self._app_provider = app_provider
        # No `tool_names` here: `revise`/`revert` only ever FILE a card. The
        # door a proposal goes through (a step naming a tool the runtime does
        # not have, a credential-bearing path) is checked once, at apply
        # time, by whoever applies the card — the watcher under `free`, the
        # operator's route under `max` — which is `_apply_skill_refinement`
        # in `mirror/server/routes/workspace.py`, and it resolves the live
        # registry itself. A second copy of that resolution here would be a
        # snapshot from whenever this tool was constructed rather than the
        # registry as it stands when the card is actually applied.

    @property
    def name(self) -> str:
        return "skill_refine"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SkillRefineInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        # `permissions.yaml` decides, per mode and per card kind. See the
        # module docstring.
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SkillRefineInput)
            else SkillRefineInput(**tool_input.model_dump())
        )
        if inp.action == "report":
            return await self._report(inp, context)
        if inp.action == "revise":
            return await self._revise(inp, context)
        if inp.action == "revert":
            return await self._revert(inp, context)
        if inp.action == "retire":
            return await self._retire(inp, context)
        return ToolResult(
            output=f"Unknown action {inp.action!r}.", is_error=True, caller_error=True,
        )

    # ── report ───────────────────────────────────────────────────

    async def _report(self, inp: SkillRefineInput, context: ToolContext) -> ToolResult:
        name = inp.name.strip()
        folder = self._skills_dir / name
        entry = load_skill_folder(folder)
        if entry is None:
            return ToolResult(
                output=f"No skill {name!r} here to report on.",
                is_error=True,
                caller_error=True,
            )
        version = inp.version.strip() or entry.version
        session_id = context.session_id or ""
        turn_id = context.turn_id or ""
        run_id = context.run_id or ""
        log_correction(
            name, session_id, version=version, turn_id=turn_id or run_id, step=inp.step,
        )
        brief = _fixer_brief(
            name=name,
            folder=folder,
            version=version,
            step=inp.step,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            record_path=_resolve_turn_record_path(turn_id),
        )
        started, message = await _spawn_fixer(context, brief)
        lead = f"Reported {name}" + (f" v{version}" if version else "") + "."
        return ToolResult(
            output=f"{lead} {message}",
            metadata={"fixer_spawned": started},
            receipt=Receipt(kind="record", id=name, locator=str(folder / SKILL_FILENAME)),
        )

    # ── revise ───────────────────────────────────────────────────

    async def _revise(self, inp: SkillRefineInput, context: ToolContext) -> ToolResult:
        name = inp.name.strip()
        if not inp.proposed_markdown.strip():
            return ToolResult(
                output="proposed_markdown must not be empty.",
                is_error=True,
                caller_error=True,
            )
        if not inp.base_sha256.strip():
            return ToolResult(
                output=(
                    "base_sha256 is required: the sha256 of the live "
                    "SKILL.md you read before writing this proposal."
                ),
                is_error=True,
                caller_error=True,
            )
        entry = load_skill_folder(self._skills_dir / name)
        if entry is None:
            return ToolResult(
                output=f"No skill {name!r} here to revise.",
                is_error=True,
                caller_error=True,
            )
        current = _read_current(self._skills_dir, name)
        base = inp.base_sha256.strip().lower()
        if _file_sha256(self._skills_dir, name) != base:
            # Refused here, where the proposer can read the file again, rather
            # than on a card the operator approves and is then told is stale.
            return ToolResult(
                output=(
                    f"{name}'s SKILL.md is not the file base_sha256 describes: it "
                    "changed since you read it, or the hash was computed over "
                    "something else. Read the live file again, rewrite from it, "
                    "and send the sha256 of exactly those bytes."
                ),
                is_error=True,
                caller_error=True,
            )
        event = WorkspaceEvent.new(
            kind="skill_refinement",
            source="agent",
            title=f"Skill revised: {name}" + (f" v{entry.version}" if entry.version else ""),
            summary=inp.rationale,
            payload={
                "name": name,
                "version": entry.version,
                "current_markdown": current,
                "proposed_markdown": inp.proposed_markdown,
                "base_sha256": base,
                "rationale": inp.rationale,
                "origin": "revise",
            },
        )
        return await self._file_card(event, context, folder=self._skills_dir / name)

    # ── revert ───────────────────────────────────────────────────

    async def _revert(self, inp: SkillRefineInput, context: ToolContext) -> ToolResult:
        name = inp.name.strip()
        folder = self._skills_dir / name
        live = load_skill_folder(folder)
        if live is None:
            return ToolResult(
                output=f"No skill {name!r} here to revert.",
                is_error=True,
                caller_error=True,
            )
        history = list_history(folder)  # oldest first
        if not history:
            return ToolResult(
                output=f"{name} has no earlier kept revision to revert to.",
                is_error=True,
                caller_error=True,
            )
        target = inp.to_version.strip()
        if target:
            match = next((h for h in history if h.version == target), None)
            if match is None:
                have = ", ".join(h.version for h in history)
                return ToolResult(
                    output=f"{name} has no kept revision {target!r}. Kept: {have}.",
                    is_error=True,
                    caller_error=True,
                )
        else:
            match = history[-1]  # newest archived predecessor
        proposed_markdown = _read_history_md(folder, match.version)
        if not proposed_markdown:
            return ToolResult(
                output=f"Could not read the kept revision {match.version!r} of {name}.",
                is_error=True,
            )
        current = _read_current(self._skills_dir, name)
        event = WorkspaceEvent.new(
            kind="skill_refinement",
            source="agent",
            title=f"Revert {name} to v{match.version}",
            summary=(
                f"Restoring the kept revision v{match.version} as the live "
                f"skill. v{live.version} stays under history/."
            ),
            payload={
                "name": name,
                "version": live.version,
                "to_version": match.version,
                "current_markdown": current,
                "proposed_markdown": proposed_markdown,
                "base_sha256": _file_sha256(self._skills_dir, name),
                "origin": "revert",
            },
        )
        return await self._file_card(event, context, folder=folder)

    # ── retire ───────────────────────────────────────────────────

    async def _retire(self, inp: SkillRefineInput, context: ToolContext) -> ToolResult:
        name = inp.name.strip()
        folder = self._skills_dir / name
        live = load_skill_folder(folder)
        if live is None:
            return ToolResult(
                output=f"No skill {name!r} here to retire.",
                is_error=True,
                caller_error=True,
            )
        if live.status == "retired":
            return ToolResult(
                output=f"{name} is already retired.",
                is_error=True,
                caller_error=True,
            )
        current = _read_current(self._skills_dir, name)
        event = WorkspaceEvent.new(
            kind="skill_retirement",
            source="agent",
            title=f"Retire {name}" + (f" v{live.version}" if live.version else ""),
            summary=inp.rationale or f"{name} is proposed for retirement.",
            payload={
                "name": name,
                "live_version": live.version,
                "current_markdown": current,
                "rationale": inp.rationale,
            },
        )
        if self._event_store is None:
            return ToolResult(
                output="No workspace store is wired here; the request could not be filed.",
                is_error=True,
            )
        try:
            self._event_store.append_event(event)
        except Exception:
            logger.exception("skill_refine: filing the retirement card failed for %s", name)
            return ToolResult(output="Could not file the retirement request.", is_error=True)
        await _broadcast(self._app_provider, event)
        return ToolResult(
            output=(
                f"Asked whether to retire {name}. Only the operator's answer "
                f"decides ({event.event_id})."
            ),
            receipt=Receipt(kind="record", id=event.event_id, locator=str(folder / SKILL_FILENAME)),
        )

    # ── shared ───────────────────────────────────────────────────

    async def _file_card(
        self, event: WorkspaceEvent, context: ToolContext, *, folder: Path,
    ) -> ToolResult:
        """File a `skill_refinement` proposal card. Never applies it: the
        watcher (`mirror/server/workspace_watch.py`) applies it under the
        posture `permissions.yaml` names, and the operator applies it by
        hand otherwise."""
        del context
        if self._event_store is None:
            return ToolResult(
                output="No workspace store is wired here; the proposal could not be filed.",
                is_error=True,
            )
        try:
            self._event_store.append_event(event)
        except Exception:
            logger.exception(
                "skill_refine: filing the proposal card failed for %s",
                (event.payload or {}).get("name"),
            )
            return ToolResult(output="Could not file the proposal.", is_error=True)
        await _broadcast(self._app_provider, event)
        return ToolResult(
            output=(
                f"Filed {event.event_id}: {event.title}. It applies once the "
                "posture and the live file both match what it was written against."
            ),
            receipt=Receipt(kind="record", id=event.event_id, locator=str(folder / SKILL_FILENAME)),
        )


def _read_current(skills_dir: Path, name: str) -> str:
    try:
        return (skills_dir / name / SKILL_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_history_md(folder: Path, version: str) -> str:
    try:
        return (folder / SKILL_HISTORY_DIRNAME / version / SKILL_FILENAME).read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""


def _file_sha256(skills_dir: Path, name: str) -> str:
    """The sha256 of the live SKILL.md's bytes, as they sit on disk.

    Bytes, not decoded text: reading text folds CRLF line endings, so a hash a
    proposer computed over the file itself never matched one computed here.
    """
    try:
        return hashlib.sha256((skills_dir / name / SKILL_FILENAME).read_bytes()).hexdigest()
    except OSError:
        return ""


async def _broadcast(
    app_provider: Optional[Callable[[], Any]], event: WorkspaceEvent,
) -> None:
    if app_provider is None:
        return
    try:
        app = app_provider()
        if app is not None:
            await broadcast_workspace_event(app, event)
    except Exception:
        logger.warning("skill_refine: broadcast failed for %s", event.event_id, exc_info=True)


def _resolve_turn_record_path(turn_id: str) -> str:
    """Best-effort: where this turn's record lives on disk, or "".

    Tried while the turn is still open (the common case: `report` runs mid
    turn, in the turn that hit the trouble) and, failing that, searched
    across the closed, dated directories. Never raises — a fixer told where
    to look is better than a tool call that failed over a pointer.
    """
    if not turn_id:
        return ""
    try:
        from tesseract.orchestrator.turns.manifest import TurnManifestStore, turns_root

        store = TurnManifestStore()
        open_path = store.open_path(turn_id)
        if open_path.exists():
            return str(open_path)
        root = turns_root()
        if not root.is_dir():
            return ""
        day_dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name != "open"),
            reverse=True,
        )
        for day_dir in day_dirs:
            candidate = day_dir / f"{turn_id}.json"
            if candidate.exists():
                return str(candidate)
    except OSError:
        pass
    return ""


def _fixer_brief(
    *,
    name: str,
    folder: Path,
    version: str,
    step: int | None,
    session_id: str,
    turn_id: str,
    run_id: str,
    record_path: str,
) -> str:
    """Pointers only. No free text from the reporting agent:
    `SkillRefineInput`'s `report` fields are `name`, `version` and `step`,
    and this is the only place those three, plus what the turn's own record
    already carries, reach the fixer."""
    return "\n".join([
        "A skill was followed and something went wrong.",
        "",
        f"Skill: {name}",
        f"Folder: {folder}",
        f"Version reported: {version or '(not given; read the live SKILL.md)'}",
        f"Step reported: {step if step is not None else '(not given)'}",
        f"Session: {session_id or '(none)'}",
        f"Turn: {turn_id or '(none)'}",
        f"Run: {run_id or '(none)'}",
        f"Turn record: {record_path or '(not found on disk; look under the runtime turns directory for this session)'}",
        "",
        "Read the turn record and the skill's SKILL.md before deciding "
        "anything — not an account of what went wrong, because that account "
        "cannot tell you whether the procedure was wrong or was not "
        "followed. Then either call skill_refine with action revise, naming "
        "the sha256 of the SKILL.md you read as base_sha256, or stop and say "
        "why you are not revising it. Write at most one revision.",
    ])


async def _spawn_fixer(context: ToolContext, brief: str) -> tuple[bool, str]:
    """Hand the brief to the `skill-fixer` agent in the background, through
    the same path a model's own `invoke_agent` call takes
    (`kernel/tools/invoke_agent.py::InvokeAgentTool`), rather than a second
    spawner. Returns (started, a sentence for the operator/model)."""
    registry = getattr(context, "spawns", None)
    provider = context.tool_registry_provider if context is not None else None
    tool_registry = provider() if provider is not None else None
    invoke_tool = tool_registry.get("invoke_agent") if tool_registry is not None else None
    if registry is None or invoke_tool is None:
        return False, (
            "The fix was not started: no background agent capability is "
            "available here. File a revision by hand with `skill_refine` "
            "action `revise` once you know what should change."
        )
    from tesseract.kernel.tools.invoke_agent import InvokeAgentInput

    try:
        result = await invoke_tool.run(
            InvokeAgentInput(name="skill-fixer", task=brief, background=True), context,
        )
    except Exception:
        logger.exception("skill_refine: spawning the fixer failed")
        return False, "The fix was not started: spawning the reviser raised an error."
    if result.is_error:
        return False, f"The fix was not started: {result.output}"
    return True, result.output
