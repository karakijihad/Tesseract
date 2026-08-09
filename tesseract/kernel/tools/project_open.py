"""project_open — make a registered project the active one.

Sets ``active_id``, refreshes ``last_active_at``, re-marks the root trusted and
re-runs MCP provisioning. Provisioning is idempotent by design, so re-running
it on every open is how a project stays reachable after a CLI config is reset,
a token is rotated, or the hub moves port.

The active project is what the ``# Active project`` prompt block renders and
what a lane opens in when its caller names no working directory — so this is
the one call that moves "where the work is".
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class ProjectOpenInput(BaseModel):
    project_id: str = Field(
        description="Registered project id, as shown by project_list (e.g. 'proj-tesseract')."
    )


class ProjectOpenTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    @property
    def name(self) -> str:
        return "project_open"

    @property
    def description(self) -> str:
        return (
            "Make a registered project active: it becomes the project named in "
            "the system prompt and the default working directory for new lanes. "
            "Also re-marks the root trusted and re-wires the claude/codex CLIs "
            "to the hub. Use project_list to see registered ids."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ProjectOpenInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ProjectOpenInput = tool_input  # type: ignore[assignment]

        from tesseract.orchestrator.projects.provisioning import trust_and_provision
        from tesseract.orchestrator.seal_guard import SealViolation, assert_cwd_outside_seal
        from tesseract.orchestrator.projects.store import (
            ProjectStore,
            ProjectStoreError,
            UnknownProjectError,
        )

        store = ProjectStore()
        try:
            project = store.get(inp.project_id)
        except ProjectStoreError as exc:
            return ToolResult(output=f"project_open: {exc}", is_error=True)
        if project is None:
            return ToolResult(
                output=(
                    f"project_open: no project registered with id {inp.project_id!r}. "
                    "Run project_list to see what is registered."
                ),
                is_error=True,
            )

        root = Path(project.root)
        if not root.is_dir():
            return ToolResult(
                output=(
                    f"project_open: {project.name} is registered at {project.root}, "
                    "which no longer exists. Re-run project_link against its new "
                    "location, or remove it from the registry."
                ),
                is_error=True,
            )

        # Activate first. Provisioning grants trust and rewrites CLI config;
        # doing it ahead of a set_active that then fails would leave the machine
        # wired for a project the registry never switched to.
        try:
            opened = store.set_active(inp.project_id)
        except (UnknownProjectError, ProjectStoreError, OSError) as exc:
            return ToolResult(output=f"project_open: {exc}", is_error=True)

        try:
            # project_link and project_new both refuse a sealed root; without
            # the same check here a hand-edited registry could make one active,
            # and every default-cwd lane would then fail at spawn with the
            # refusal coming from somewhere the operator never called.
            assert_cwd_outside_seal(root)
        except SealViolation as exc:
            return ToolResult(output=f"project_open: {exc}", is_error=True)

        provision_note = await trust_and_provision(root)

        lines = [f"Active project: {opened.name} ({opened.id}) at {opened.root}", provision_note]
        if opened.vcs.git:
            lines.append(
                f"git: {opened.vcs.default_branch or '?'} @ "
                f"{opened.vcs.remote or 'no remote'}"
            )
        for label, cmd in (
            ("test", opened.verify.test),
            ("typecheck", opened.verify.typecheck),
            ("lint", opened.verify.lint),
        ):
            if cmd:
                lines.append(f"verify.{label}: {cmd}")
        if opened.conventions_file:
            lines.append(f"conventions: {opened.conventions_file}")

        return ToolResult(output="\n".join(lines), metadata=opened.model_dump(mode="json"))
