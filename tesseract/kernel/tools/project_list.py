"""project_list — read-only enumeration of registered projects.

Pure read of ``<TESSERACT_HOME>/projects/registry.json``. Use it before
``project_open`` to see what is registered and which one is active.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class ProjectListInput(BaseModel):
    pass


class ProjectListTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "project_list"

    @property
    def description(self) -> str:
        return (
            "List every registered project with its root, git identity and "
            "verification commands, and say which one is active. Read-only. "
            "Use before project_open to see what is available."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ProjectListInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.orchestrator.projects.store import ProjectStore, ProjectStoreError

        store = ProjectStore()
        try:
            projects, active = store.snapshot()
        except ProjectStoreError as exc:
            return ToolResult(output=f"project_list: {exc}", is_error=True)
        # `active` is resolved against the roster, so an id pointing at a
        # project that is no longer registered reads as "none selected" rather
        # than printing a list with neither a marker nor the line saying why.
        active_id = active.id if active is not None else None

        if not projects:
            return ToolResult(
                output=(
                    "No projects registered. Use project_link on an existing "
                    "directory, or project_new to start one."
                ),
                metadata={"projects": [], "active_id": None},
            )

        lines: list[str] = []
        for project in projects:
            marker = "* " if project.id == active_id else "  "
            lines.append(f"{marker}{project.id} — {project.name} — {project.root}")
            if project.vcs.git:
                branch = project.vcs.default_branch or "?"
                lines.append(f"    git: {branch} @ {project.vcs.remote or 'no remote'}")
            for label, cmd in (
                ("test", project.verify.test),
                ("typecheck", project.verify.typecheck),
                ("lint", project.verify.lint),
            ):
                if cmd:
                    lines.append(f"    verify.{label}: {cmd}")
        if active_id is None:
            lines.append("\nNo active project. Use project_open to select one.")

        return ToolResult(
            output="\n".join(lines),
            metadata={
                "active_id": active_id,
                "projects": [p.model_dump(mode="json") for p in projects],
            },
        )
