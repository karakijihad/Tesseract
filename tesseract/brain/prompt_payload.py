"""What one turn costs before the first message.

A readout of `prompt.SECTIONS` — every block the assembly appends and what it
built to — in the three groups the payload actually divides into:

    instructions + tools + memory = what rides every turn

Nothing is dropped, so this is a composition rather than a verdict: the whole
of it is sent, every turn, and the number is a thing to optimise on purpose.
This panel is where that number is read.

It measures by ASSEMBLING, not by estimating. `resolve_inputs`,
`build_sections` and `join_sections` are the same three calls
`assemble_system_prompt` makes, in the same order, so a figure here is the
figure the NEXT turn would send. A second implementation that added up sizes
its own way would be a readout free to disagree with the prompt it describes.

"The next turn", and not "the turn in front of you", because the whole head is
held for the life of a conversation (`chat.py::_head_for_turn`): every row
above the boundary is read once when a conversation begins and kept, so a
conversation that has been running while a consolidation job wrote, or since
before an operator edit, is carrying an older copy than the one measured here.
This panel is asked per SURFACE and not per conversation — a channel has many
— so it has no conversation to report on and does not pretend to. It answers
the question it is actually for: what is the prompt made of, and what is worth
shrinking. Nothing you would act on differs: a held copy and a fresh one are
bounded by the same caps and differ in age rather than in size.

**Resident is not the same as retrievable.** The memory group is what is
INLINE — the capsule's curated slice, not the store. `residency` reports the
difference, because "the assistant knows this" and "the assistant can find
this if it thinks to look" are different claims and the panel should not blur
them.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from tesseract.brain import prompt as prompt_module
from tesseract.brain.prompt_content import (
    SOURCE_ROLLUPS_TO_LOAD,
    TOPIC_HUBS_TO_LOAD,
)

logger = logging.getLogger(__name__)

#: Chars per token. An ESTIMATE, and deliberately the same one every adapter's
#: `count_tokens` uses — Anthropic, OpenAI, Gemini and the CLI adapters all
#: divide by four, and that estimate is what decides when a session compacts.
#: A more accurate count here would be a readout disagreeing with the mechanism
#: it describes. The real figure only exists after a round trip, in
#: `StreamChunk.raw["usage"]`.
CHARS_PER_TOKEN = 4

def tokens(chars: int) -> int:
    return chars // CHARS_PER_TOKEN


@dataclass(frozen=True)
class SectionCost:
    """One block of the prompt, sized."""

    name: str
    #: A filename for an inlined document, a short noun phrase otherwise.
    label: str
    #: One plain sentence saying what the block is.
    description: str
    #: ``instructions`` · ``tools`` · ``memory``.
    group: str
    chars: int
    tokens: int
    #: The workspace file this section inlines whole, if it is one.
    document: str | None
    #: Cockpit view that shows where this came from, or ``None``.
    panel: str | None
    hint: str | None


def payload_breakdown(
    *,
    channel_name: str | None = None,
    tool_registry_provider: Callable[[], Any] | None = None,
    workspace_dir: Path | None = None,
    memory_store_dir: Path | None = None,
) -> dict[str, Any]:
    """Assemble one turn's payload and report its composition.

    ``channel_name`` selects the surface — ``None`` is the cockpit. Blocking:
    it reads the workspace and the memory store exactly as a turn does, so
    call it off the event loop.
    """
    ctx = prompt_module.resolve_inputs(
        workspace_dir=workspace_dir,
        memory_store_dir=memory_store_dir,
        channel_name=channel_name,
        # `failures_scope` is left to resolve. A measurement runs outside any
        # chat session, so `active_scope()` finds none and the digest renders
        # without a streak line — which is what a turn with no history
        # carries too.
        tool_registry_provider=tool_registry_provider,
    )
    built = prompt_module.build_sections(ctx)
    assembled = prompt_module.join_sections(built)
    schema_chars = prompt_module._core_schema_chars(ctx.registry)

    sections = [
        SectionCost(
            name=section.name,
            label=section.display_label,
            description=section.description,
            group=section.group,
            chars=len(built.get(section.name, "")),
            tokens=tokens(len(built.get(section.name, ""))),
            document=section.document,
            panel=section.origin,
            hint=section.origin_hint,
        )
        for section in prompt_module.SECTIONS
    ]

    # The schemas are the rest of the tool cost, and they are a ROW rather than
    # a figure the view knows to add: they are not part of the prompt string,
    # which is exactly why they went unwatched for so long, and a readout that
    # left the reader to remember them would repeat that.
    tool_count = _core_tool_count(ctx.registry)
    sections.append(
        SectionCost(
            name="schemas",
            label="tool schemas",
            description=(
                f"The full callable contract for each of the {tool_count} tools "
                "in the working set: every argument, its type, and when to use "
                "it. Sent beside the prompt, not inside it."
            ),
            group="tools",
            chars=schema_chars,
            tokens=tokens(schema_chars),
            document=None,
            # The working set decides WHICH tools are described, so it decides
            # this row's size. Settings -> Tools decides what each may do,
            # which changes nothing here.
            panel="conscience",
            hint="Usage -> Working set",
        )
    )

    prose_by_group = {name: 0 for name in prompt_module.GROUPS}
    for cost in sections:
        prose_by_group[cost.group] += cost.chars

    # The join adds a blank line between adjacent non-empty sections, so the
    # groups come to slightly less than the assembled length. The total is the
    # assembled string plus the schemas — what the model actually receives.
    total_chars = len(assembled) + schema_chars
    return {
        "surface": channel_name or "cockpit",
        "total_chars": total_chars,
        "total_tokens": tokens(total_chars),
        "prose_chars": len(assembled),
        "schema_chars": schema_chars,
        "core_tools": tool_count,
        "groups": [
            {
                "name": name,
                "chars": prose_by_group[name],
                "tokens": tokens(prose_by_group[name]),
            }
            for name in prompt_module.GROUPS
        ],
        "sections": [asdict(s) for s in sections],
        "residency": _residency(ctx.memory_store),
    }


def _residency(memory_store: Path) -> dict[str, Any]:
    """How much of the store rides the turn, and how much only `memory_search`
    can reach.

    The capsule inlines MEMORY.md, two days of captures, and the freshest few
    derived trees — three topic hubs out of however many exist. Without this
    the panel implies the assistant carries its whole memory, which is the
    single most misleading thing a context readout can say.
    """
    def _count(*parts: str) -> int:
        directory = memory_store.joinpath(*parts)
        try:
            return sum(1 for _ in directory.glob("*.md"))
        except OSError:
            return 0

    return {
        "topic_hubs": _count("trees", "topic"),
        "topic_hubs_inlined": TOPIC_HUBS_TO_LOAD,
        "source_rollups": _count("trees", "source"),
        "source_rollups_inlined": SOURCE_ROLLUPS_TO_LOAD,
        "daily_captures": _count("daily"),
    }


def _core_tool_count(registry: Any) -> int:
    """How many schemas make up `schema_chars`.

    Decoration beside the measured figure, so it fails to zero rather than
    taking the readout down — same fail-open contract as every prompt builder.
    """
    if registry is None:
        return 0
    try:
        return len(list(registry.schemas_for_adapter(enabled_extended=set())))
    except Exception:
        logger.exception("payload: could not count the core schemas")
        return 0
