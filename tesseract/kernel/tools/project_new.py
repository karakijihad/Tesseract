"""project_new — interview, create, register, open a new project.

**Two calls, not one.** ``ask_clarification`` is asynchronous by construction:
it posts a workspace card and the operator's answer arrives on a later turn.
So the first call proposes — exact path, git, remote, verify commands — and
creates nothing. The second call carries ``confirmed=true`` plus whatever the
operator corrected, and only then does anything land on disk.

That shape is also what makes the remote safe. A GitHub repository is
outward-facing and is not undone by deleting a local directory, so it is
created only when ``create_remote`` and ``confirmed`` are both true in the same
call — a call the operator sees at the ASK gate with the flag visible.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_S = 30
# `gh repo create --push` uploads. Longer than a local git call, still bounded:
# a hang here holds the turn.
_GH_TIMEOUT_S = 120

# A project directory is one path component. Both separators, on every OS —
# a Windows-authored name reaching a posix host must fail the same way.
_SEPARATORS = re.compile(r"[\\/]")

_STARTER_CONVENTIONS = """# {name}

## What this is

<one paragraph — replace this>

## Conventions

- <language / framework choices>
- <test and lint expectations>

## Verify

| Check | Command |
|---|---|
{verify_rows}

This file is the cross-tool conventions file: Codex reads `AGENTS.md`
natively and Claude Code reads it too, so one file serves every agent that
works here.
"""


class ProjectNewInput(BaseModel):
    name: str = Field(description="Project name. Also the directory name unless overridden.")
    parent_dir: str | None = Field(
        default=None,
        description=(
            "Directory to create the project inside. Defaults to the active "
            "project's parent — proposed for confirmation, never assumed."
        ),
    )
    dir_name: str | None = Field(
        default=None, description="Directory name, when it should differ from `name`."
    )
    git_init: bool = Field(default=True, description="Initialise a git repository.")
    create_remote: bool = Field(
        default=False,
        description=(
            "Create a GitHub repository and push to it. Outward-facing and not "
            "reversible by deleting the local directory — requires an explicit "
            "yes from the operator in this same call, plus an authenticated "
            "`gh`. Leave false unless they said yes."
        ),
    )
    remote_visibility: Literal["private", "public"] = Field(
        default="private", description="Visibility of the created GitHub repository."
    )
    test: str | None = Field(default=None, description="Test command to record.")
    typecheck: str | None = Field(default=None, description="Type-check command to record.")
    lint: str | None = Field(default=None, description="Lint command to record.")
    confirmed: bool = Field(
        default=False,
        description=(
            "False (default) posts the proposal to the operator and creates "
            "nothing. Set true only after they have answered, and carry their "
            "corrections in the other fields."
        ),
    )


class ProjectNewTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "projects"
    summary: ClassVar[str] = "Start a new project from scratch: create the directory, register, open it."
    use_when: ClassVar[str] = (
        "Use when the directory does not exist yet. Call first without "
        "`confirmed` to propose the plan; call again with confirmed=true to "
        "create it."
    )
    not_when: ClassVar[str] = (
        "for a directory that already exists, use `project_link` instead."
    )

    def __init__(
        self,
        store: EventStore,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._store = store
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "project_new"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ProjectNewInput

    # --- helpers --------------------------------------------------------

    def _resolve_target(self, inp: ProjectNewInput) -> tuple[Path | None, str]:
        """(target, explanation). ``None`` means there is nothing to propose."""
        from tesseract.orchestrator.projects.store import ProjectStore

        dir_name = (inp.dir_name or inp.name).strip()
        if not dir_name:
            return None, "project_new: name is empty"
        # One path component, always. A `..` segment resolves out of the parent
        # the operator was shown, and the ASK gate sees the raw inputs rather
        # than the resolved path — so the traversal would not be visible at the
        # only point it could be refused.
        if dir_name in (".", "..") or _SEPARATORS.search(dir_name):
            return None, (
                f"project_new: {dir_name!r} is not a directory name — it must "
                "be a single path component, with no separators and no '..'. "
                "Use parent_dir to say where the project lives."
            )
        if inp.parent_dir:
            parent = Path(inp.parent_dir).expanduser()
            source = "as given"
        else:
            try:
                active = ProjectStore().active()
            except Exception as exc:  # noqa: BLE001 — surfaced to the operator
                return None, f"project_new: project registry unreadable ({exc})"
            if active is None:
                return None, (
                    "project_new: no parent_dir given and no active project to "
                    "infer one from. Pass parent_dir explicitly."
                )
            parent = Path(active.root).parent
            source = f"alongside {active.name}"
        try:
            return (parent / dir_name).resolve(), source
        except (OSError, ValueError) as exc:
            return None, f"project_new: cannot resolve target path ({exc})"

    async def _post_proposal(
        self, inp: ProjectNewInput, target: Path, source: str, context: ToolContext
    ) -> ToolResult:
        verify_lines = [
            f"- {label}: {cmd}"
            for label, cmd in (("test", inp.test), ("typecheck", inp.typecheck), ("lint", inp.lint))
            if cmd
        ] or ["- none yet — the verification gate will have nothing to run"]
        question = (
            f"Start a new project '{inp.name}'?\n\n"
            f"- location: {target}  ({source})\n"
            f"- git repo: {'yes' if inp.git_init else 'no'}\n"
            f"- GitHub remote: {'yes — ' + inp.remote_visibility if inp.create_remote else 'no'}\n"
            "- verify commands:\n  " + "\n  ".join(verify_lines) + "\n\n"
            "Answer with any corrections, or 'go' to accept as proposed."
        )
        event = WorkspaceEvent.new(
            kind="clarification",
            source="agent",
            title=f"New project: {inp.name}?"[:200],
            summary=question[:1200],
            payload={
                "question": question,
                "context": "project_new proposal — nothing has been created yet.",
                "urgency": "normal",
                "session_id": context.session_id,
                "proposal": {
                    "name": inp.name,
                    "target": str(target),
                    "git_init": inp.git_init,
                    "create_remote": inp.create_remote,
                    "verify": {"test": inp.test, "typecheck": inp.typecheck, "lint": inp.lint},
                },
            },
            priority=5,
            author_id="agent",
            author_display="Agent",
        )
        try:
            self._store.append_event(event)
        except OSError as exc:
            logger.exception("project_new: proposal append failed")
            return ToolResult(output=f"project_new: could not post proposal ({exc})", is_error=True)
        if self._app_provider is not None:
            app = self._app_provider()
            if app is not None:
                await broadcast_workspace_event(app, event)
        return ToolResult(
            output=(
                f"Proposed — nothing created yet.\n{question}\n\n"
                f"Posted as clarification {event.event_id}. When the operator "
                "answers, call project_new again with confirmed=true and their "
                "corrections."
            ),
            metadata={"event_id": event.event_id, "target": str(target), "created": False},
        )

    # --- run ------------------------------------------------------------

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ProjectNewInput = tool_input  # type: ignore[assignment]

        from tesseract.orchestrator.projects.models import (
            Project,
            VerifyCommands,
            mint_project_id,
        )
        from tesseract.orchestrator.projects.provisioning import trust_and_provision
        from tesseract.orchestrator.projects.store import ProjectStore, ProjectStoreError
        from tesseract.orchestrator.seal_guard import SealViolation, assert_cwd_outside_seal

        target, source = self._resolve_target(inp)
        if target is None:
            return ToolResult(output=source, is_error=True)
        try:
            assert_cwd_outside_seal(target)
        except SealViolation as exc:
            return ToolResult(output=f"project_new: {exc}", is_error=True)
        if target.exists() and not target.is_dir():
            return ToolResult(
                output=f"project_new: {target} already exists and is not a directory",
                is_error=True,
            )
        try:
            occupied = target.is_dir() and any(target.iterdir())
        except OSError as exc:
            return ToolResult(
                output=f"project_new: cannot inspect {target} ({exc})", is_error=True
            )
        if occupied:
            return ToolResult(
                output=(
                    f"project_new: {target} already exists and is not empty. "
                    "Use project_link to register it instead."
                ),
                is_error=True,
            )

        if not inp.confirmed:
            return await self._post_proposal(inp, target, source, context)

        # Seeded, not used as an either/or fallback: on the common success
        # path `notes` is non-empty (git init) and the directory + AGENTS.md
        # would drop out of the failure message exactly when there is most to
        # report.
        notes: list[str] = [f"created {target}"]
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(output=f"project_new: could not create {target} ({exc})", is_error=True)

        verify = VerifyCommands(test=inp.test, typecheck=inp.typecheck, lint=inp.lint)
        conventions_path = target / "AGENTS.md"
        verify_rows = "\n".join(
            f"| {label} | `{cmd}` |"
            for label, cmd in (("test", verify.test), ("typecheck", verify.typecheck), ("lint", verify.lint))
            if cmd
        ) or "| — | not set yet |"
        try:
            conventions_path.write_text(
                _STARTER_CONVENTIONS.format(name=inp.name, verify_rows=verify_rows),
                encoding="utf-8",
            )
            notes.append("wrote AGENTS.md")
        except OSError as exc:
            notes.append(f"AGENTS.md NOT written ({exc})")

        if inp.git_init:
            ok, note = await asyncio.to_thread(_git_init, target)
            notes.append(note)
            if ok and inp.create_remote:
                notes.append(
                    await asyncio.to_thread(
                        _create_remote, target, inp.dir_name or inp.name, inp.remote_visibility
                    )
                )
            elif inp.create_remote:
                notes.append("remote skipped — git init failed, nothing to push")
        elif inp.create_remote:
            notes.append("remote skipped — a GitHub remote needs a git repository (git_init=false)")

        from tesseract.orchestrator.projects.detect import detect_vcs

        artifacts = ", ".join(notes)

        store = ProjectStore()
        try:
            existing_ids = {p.id for p in store.list_projects()}
        except ProjectStoreError as exc:
            return ToolResult(
                output=(
                    f"project_new: registry unreadable ({exc}). Nothing was "
                    f"registered — already done on disk: {artifacts}."
                ),
                is_error=True,
            )

        project = Project(
            id=mint_project_id(inp.name, existing_ids),
            name=inp.name,
            root=str(target),
            # Threaded: git shells out, and the loop must stay responsive.
            vcs=await asyncio.to_thread(detect_vcs, target),
            verify=verify,
            conventions_file="AGENTS.md" if conventions_path.exists() else None,
        )
        # Register before trusting, so a registry failure does not leave a
        # standing trust grant behind for a project that is not on the books.
        # Registration and activation are two separate writes, so they get two
        # separate handlers: reporting "registration failed" after it succeeded
        # sends the operator looking for a project that is already on the books,
        # and a retry then refuses the now-non-empty directory.
        try:
            saved = store.register(project)
        except (ProjectStoreError, OSError) as exc:
            return ToolResult(
                output=(
                    f"project_new: registration failed ({exc}). {target} is "
                    f"NOT registered — already done there: {artifacts}. "
                    "Remove it or re-run project_link once the registry is "
                    "writable."
                ),
                is_error=True,
            )
        try:
            opened = store.set_active(saved.id)
        except (ProjectStoreError, OSError) as exc:
            return ToolResult(
                output=(
                    f"project_new: {saved.name} IS registered as {saved.id} at "
                    f"{saved.root}, but activating it failed ({exc}). Do not "
                    f"re-run project_new — use project_open('{saved.id}')."
                ),
                is_error=True,
            )

        provision_note = await trust_and_provision(target)

        lines = [
            f"Created and opened {opened.name} ({opened.id}) at {opened.root}",
            provision_note,
            *notes,
        ]
        return ToolResult(output="\n".join(lines), metadata={**opened.model_dump(mode="json"), "created": True})


def _run(args: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — args list, no shell
        args, cwd=str(cwd), capture_output=True, text=True, check=False, timeout=timeout
    )


def _git_init(target: Path) -> tuple[bool, str]:
    try:
        result = _run(["git", "init"], cwd=target, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git init failed ({exc})"
    if result.returncode != 0:
        return False, f"git init failed: {result.stderr.strip()}"
    return True, "git repository initialised"


def _create_remote(target: Path, repo_name: str, visibility: str) -> str:
    """Create a GitHub repo and push. Reached only with an explicit yes.

    Every failure returns a note rather than raising: the local project is
    already created and registered by this point, and losing that to a `gh`
    problem would be the worse outcome. The operator can create the remote by
    hand from a tree that already exists.
    """
    try:
        auth = _run(["gh", "auth", "status"], cwd=target, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"remote not created — gh unavailable ({exc})"
    if auth.returncode != 0:
        return "remote not created — gh is not authenticated (`gh auth login`)"
    try:
        # Checked, not fired-and-forgotten: an unconfigured user.email aborts
        # the commit, and without this the operator hears about it as whatever
        # `gh` says about pushing an empty repository.
        for args in (["git", "add", "-A"], ["git", "commit", "-m", "Initial commit"]):
            step = _run(args, cwd=target, timeout=_GIT_TIMEOUT_S)
            if step.returncode != 0:
                return (
                    f"remote not created — {' '.join(args)} failed: "
                    f"{(step.stderr or step.stdout).strip()}"
                )
        result = _run(
            [
                "gh", "repo", "create", repo_name,
                f"--{visibility}", "--source", ".", "--remote", "origin", "--push",
            ],
            cwd=target,
            timeout=_GH_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"remote not created ({exc})"
    if result.returncode != 0:
        return f"remote not created: {result.stderr.strip()}"
    return f"GitHub repository created ({visibility}) and pushed"
