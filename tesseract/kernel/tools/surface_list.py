"""surface_list — enumerate the surfaces on a canvas view. AUTO, read-only.

The counterpart the flailing-recovery loop was missing: `surface_create`
returns only a single id, so without this a caller can't see what it has
already spawned (and ends up spamming duplicates / renaming titles instead of
closing). Returns a compact id/type/title/mode listing, z-ordered.

The last column is the only one that is not the store reading back its own
record of what the caller asked for. Everything left of it confirms the card
was REGISTERED; `render` is what a client said happened when it tried to draw
it, which is the difference between a card that exists and a card the operator
can see. `unreported` is a real answer, not a gap: nothing is holding that card
on screen right now.

The `control` column is the same kind of fact and answers the question a
caller would otherwise have to guess at: what can I ask this card to do. It is
what the card SAID about itself when it drew, not a rule about its type, and
the difference matters for a framed page, where whether there is a way in
depends on the page rather than on the card. `-` means it takes nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.surfaces.store import get_surface_store


def _control_cell(report: dict[str, Any] | None) -> str:
    """What this card said it can be asked. `surface_control` is the verb.

    Three distinct answers and the model needs all three: a list of actions,
    `-` for a card that drew and takes nothing, and `unknown` for a card that
    has not reported, where the honest thing is to try rather than to assume.
    """
    if report is None or "controls" not in report:
        return "unknown"
    controls = report.get("controls") or []
    return ",".join(controls) if controls else "-"


class SurfaceListInput(BaseModel):
    view: str = Field(default="orb", description="Canvas view to list (e.g. 'orb').")


class SurfaceListTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"
    group: ClassVar[str] = "showing-the-operator"
    summary: ClassVar[str] = (
        "List surfaces on a canvas view: id, type, title, mode, and whether each one rendered."
    )
    use_when: ClassVar[str] = (
        "Before spawning to avoid duplicates, to find a surface_id, to check whether a card "
        "drew, or to see what it can be asked before asking it. Render and control are the "
        "only columns that are not your own instruction read back. Anything but mounted, say "
        "so in the reason's own words: a card that did not draw is not a card they have."
    )
    not_when: ClassVar[str] = (
        "`screen_look` for what a card actually LOOKS like. This only "
        "confirms it was registered, not a look at the pixels."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    @property
    def name(self) -> str:
        return "surface_list"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SurfaceListInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: SurfaceListInput = tool_input  # type: ignore[assignment]
        store = get_surface_store()
        rows = store.list_for_view(inp.view)
        if not rows:
            return ToolResult(output=f"no surfaces on view {inp.view!r}", metadata={"count": 0})
        now = datetime.now(timezone.utc)
        reports = {r["id"]: store.render_report(r["id"]) for r in rows}
        lines = [
            f"{r['id']} | {r['type']} | {r.get('title') or '-'} | "
            f"{r.get('mode') or 'embedded'} | {_render_cell(reports[r['id']], now)} | "
            f"{_control_cell(reports[r['id']])}"
            for r in rows
        ]
        return ToolResult(
            output=(
                f"{len(rows)} surface(s) on {inp.view!r} "
                f"(id | type | title | mode | render | control):\n" + "\n".join(lines)
            ),
            metadata={
                "count": len(rows),
                "view": inp.view,
                "render": {sid: rep for sid, rep in reports.items() if rep is not None},
            },
        )


def _render_cell(report: dict[str, Any] | None, now: datetime) -> str:
    if report is None:
        return "unreported"
    status = report.get("status", "unreported")
    detail = report.get("detail") or ""
    cell = f"{status}: {detail}" if detail else status
    age = _age(report.get("at"), now)
    return f"{cell} ({age})" if age else cell


def _age(stamp: Any, now: datetime) -> str:
    """Compact age of a report. A stale `mounted` is still a claim about the
    past, so the caller gets to judge it rather than being handed a bare word."""
    if not isinstance(stamp, str):
        return ""
    try:
        seconds = (now - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return ""
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{int(seconds // 3600)}h ago"
