"""project_budget — what a day of unattended work on a project may spend.

A project is priced when it is started (`project_new` asks for the number) and
repriced here. The two are the same field on the same record, so a budget set
from a phone and one set at the desk are one answer.

**Its posture is a floor no mode relaxes**, which `playbook_judge` and
`context_set` are the precedent for rather than `task_propose`: that one is
deliberately auto under `free`, because giving itself work is what the mode is
for. Setting its own ceiling is not. A budget the assistant can raise is not a
budget, so `permissions.yaml` names this in `modes.free.overrides` as well as
in `tools:`, and `free`'s auto baseline never reaches it.

`None` clears the price and is not zero. Zero is a number the operator chose,
and the morning honours it by proposing nothing; `None` is a project they never
priced, and the morning has nothing to honour. Both end in no work today and
they are not the same statement, so they stay distinguishable everywhere they
are rendered.

The ceiling here is per project and per day. It bounds one project's share of
`agenda.yaml::daily_caps`, never widens it: the global cap still stops
everything when it is reached, whatever a project was promised.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.receipt import Receipt
from tesseract.orchestrator.projects.models import budget_line


class ProjectBudgetInput(BaseModel):
    project_id: str = Field(
        description="Registered project id, as shown by project_list (e.g. 'proj-snake')."
    )
    budget_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "USD a day of unattended work on this project may spend. Omit it "
            "to clear the budget, which stops the project being worked "
            "unattended at all."
        ),
    )


class ProjectBudgetTool(Tool):
    # NOT a default to be tuned. The gate's prompt is where the operator agrees
    # to the number, and on a channel that tap is the same inline keyboard every
    # gated call raises.
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "projects"
    summary: ClassVar[str] = "Set what a day of unattended work on a project may spend."
    use_when: ClassVar[str] = (
        "Use when the operator names a daily spend for a project, or asks to "
        "raise, lower or remove one. Use `project_list` for ids first."
    )
    not_when: ClassVar[str] = (
        "starting a project, which takes its budget in the same call "
        "(`project_new`). This is for changing one that is already registered."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "record"
    recovery_behaviour: ClassVar[str] = "idempotent"

    @property
    def name(self) -> str:
        return "project_budget"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ProjectBudgetInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def ask_reason(self, validated: object) -> str:
        """What the operator is agreeing to, readable on a phone: the money and
        the project's name, never an id."""
        inp = validated if isinstance(validated, ProjectBudgetInput) else None
        if inp is None:
            return "change a project's daily budget"
        name = _project_name(inp.project_id)
        if inp.budget_usd is None:
            return f"stop working {name} unattended, by clearing its daily budget"
        return f"let unattended work on {name} spend {budget_line(inp.budget_usd)}"

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del context
        inp: ProjectBudgetInput = tool_input  # type: ignore[assignment]

        from tesseract.orchestrator.projects.store import (
            ProjectStore,
            ProjectStoreError,
            UnknownProjectError,
        )

        store = ProjectStore()
        try:
            updated = store.set_budget(inp.project_id, inp.budget_usd)
        except UnknownProjectError:
            return ToolResult(
                output=(
                    f"project_budget: no project registered with id "
                    f"{inp.project_id!r}. Run project_list to see what is "
                    "registered."
                ),
                is_error=True,
                caller_error=True,
            )
        except (ProjectStoreError, OSError) as exc:
            return ToolResult(output=f"project_budget: {exc}", is_error=True)

        return ToolResult(
            output=f"{updated.name}: {budget_line(updated.budget_usd)}",
            receipt=Receipt(kind="record", id=updated.id, locator=str(store.path)),
            metadata=updated.model_dump(mode="json"),
        )


def _project_name(project_id: str) -> str:
    """The project's own name for the ask, falling back to the id.

    The gate prompt is read on a phone and `proj-two-of-us-signal-spark` is not
    what the operator called it. A store that cannot be read here is not an
    error: the ask still has to render, and the id is a true if uglier answer.
    """
    from tesseract.orchestrator.projects.store import ProjectStore, ProjectStoreError

    try:
        project = ProjectStore().get(project_id)
    except (ProjectStoreError, OSError):
        return project_id
    return project.name if project is not None else project_id
