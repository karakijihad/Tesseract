"""project_link — register an existing directory as a project.

Records where it lives, its git identity and how it verifies itself, marks it
trusted, and wires the CLIs so a shell opened there reaches the hub. Does not
make it active — that is ``project_open``.

ASK-gated: it writes registry state and marks a directory trusted, which is a
standing grant rather than a one-off read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class ProjectLinkInput(BaseModel):
    root: str = Field(
        description=(
            "Path to the project directory. Must already exist. Give an "
            "absolute path — a relative one resolves against the backend's "
            "working directory, which is not where you are. The directory "
            "itself must be the repository root when it is a git repo; a "
            "subdirectory registers without git identity."
        )
    )
    name: str | None = Field(
        default=None,
        description="Display name. Defaults to the directory's own name.",
    )
    test: str | None = Field(
        default=None,
        description=(
            "Command that runs the project's tests. Overrides detection. "
            "Stored as a string and run through the normal bash policy path "
            "when the gate invokes it — never executed by this tool."
        ),
    )
    typecheck: str | None = Field(
        default=None, description="Type-check command. Overrides detection."
    )
    lint: str | None = Field(
        default=None, description="Lint command. Overrides detection."
    )


class ProjectLinkTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "projects"
    summary: ClassVar[str] = "Register an existing directory as a project."
    use_when: ClassVar[str] = (
        "Use to adopt a directory that already exists: it records root, git "
        "identity and verification commands, and marks it trusted."
    )
    not_when: ClassVar[str] = (
        "does not switch the active project — use `project_open` for that. "
        "For a directory that does not exist yet, use `project_new`."
    )

    @property
    def name(self) -> str:
        return "project_link"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ProjectLinkInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ProjectLinkInput = tool_input  # type: ignore[assignment]

        from tesseract.orchestrator.projects.detect import (
            detect_conventions_file,
            detect_vcs,
            detect_verify,
        )
        from tesseract.orchestrator.projects.models import (
            Project,
            VcsInfo,
            VerifyCommands,
            mint_project_id,
        )
        from tesseract.orchestrator.projects.provisioning import trust_and_provision
        from tesseract.orchestrator.projects.store import ProjectStore, ProjectStoreError
        from tesseract.orchestrator.seal_guard import SealViolation, assert_cwd_outside_seal

        try:
            root = Path(inp.root).expanduser().resolve()
        except (OSError, ValueError) as exc:
            return ToolResult(
                output=f"project_link: cannot resolve {inp.root!r} ({exc})", is_error=True
            )
        if not root.is_dir():
            return ToolResult(
                output=f"project_link: {root} does not exist or is not a directory",
                is_error=True,
            )
        try:
            # A lane opened in the sealed tree is refused at open. Registering
            # such a root would mint a project whose every lane fails, so the
            # refusal belongs here where it can still be explained.
            assert_cwd_outside_seal(root)
        except SealViolation as exc:
            return ToolResult(output=f"project_link: {exc}", is_error=True)

        # Detection shells out to git (up to four calls, each with its own
        # timeout) and reads files. On the loop that stalls health checks, WS
        # heartbeats and inbound turns — an unreachable `origin` is exactly the
        # case that runs to the timeout.
        # `return_exceptions=True` because detection is advisory: a
        # PermissionError from one probe must degrade to "nothing detected",
        # not propagate out of a tool whose every other failure is a reported
        # ToolResult.
        vcs, detected, conventions = await asyncio.gather(
            asyncio.to_thread(detect_vcs, root),
            asyncio.to_thread(detect_verify, root),
            asyncio.to_thread(detect_conventions_file, root),
            return_exceptions=True,
        )
        detect_notes: list[str] = []
        if isinstance(vcs, BaseException):
            detect_notes.append(f"git detection failed ({vcs})")
            vcs = VcsInfo()
        if isinstance(detected, BaseException):
            detect_notes.append(f"verify detection failed ({detected})")
            detected = VerifyCommands()
        if isinstance(conventions, BaseException):
            detect_notes.append(f"conventions detection failed ({conventions})")
            conventions = None
        verify = VerifyCommands(
            test=inp.test or detected.test,
            typecheck=inp.typecheck or detected.typecheck,
            lint=inp.lint or detected.lint,
        )

        store = ProjectStore()
        try:
            existing_ids = {p.id for p in store.list_projects()}
        except ProjectStoreError as exc:
            return ToolResult(output=f"project_link: {exc}", is_error=True)

        name = inp.name or root.name
        project = Project(
            id=mint_project_id(name, existing_ids),
            name=name,
            root=str(root),
            vcs=vcs,
            verify=verify,
            conventions_file=conventions,
        )

        # Register BEFORE trusting. `trust_and_provision` grants a standing
        # permission; doing it first would leave that grant behind for a
        # directory whose registration then failed — a durable side effect of a
        # call that reported an error.
        try:
            saved = store.register(project)
        except (ProjectStoreError, OSError) as exc:
            return ToolResult(output=f"project_link: {exc}", is_error=True)

        provision_note = await trust_and_provision(root)

        lines = [f"Registered {saved.name} ({saved.id}) at {saved.root}", provision_note]
        lines.extend(detect_notes)
        if saved.vcs.git:
            lines.append(
                f"git: {saved.vcs.default_branch or '?'} @ "
                f"{saved.vcs.remote or 'no remote'}"
            )
        else:
            lines.append("git: not a repository root")
        if verify.is_empty():
            lines.append(
                "verify: none detected — the verification gate has nothing to "
                "run until test/typecheck/lint are set."
            )
        else:
            lines.append("verify:")
            for label, cmd, given in (
                ("test", verify.test, inp.test),
                ("typecheck", verify.typecheck, inp.typecheck),
                ("lint", verify.lint, inp.lint),
            ):
                if cmd:
                    # Per command, not per call: a call that overrides only
                    # `test` must not label the detected lint as operator-given.
                    lines.append(
                        f"  {label}: {cmd} ({'as given' if given else 'detected'})"
                    )
        lines.append(f"Not active yet — project_open('{saved.id}') to switch to it.")

        return ToolResult(output="\n".join(lines), metadata=saved.model_dump(mode="json"))
