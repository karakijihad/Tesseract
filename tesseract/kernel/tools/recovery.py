"""What recovery may do with a call it does not know the outcome of.

The rule the whole thing turns on: recovery may repeat reasoning, and it must
not unknowingly repeat an external effect. Until now nothing could tell those
apart, so a run that died mid-task was closed rather than continued, which is
correct and expensive.

A tool is the only thing that knows which of the two its call is, so it says
so, once, on its class. Four answers and the set is closed:

  ``read_only``   it repeats no effect anyone else can see. Run it again.
  ``idempotent``  running it again with the same arguments leaves nothing
                  anyone has to undo. Run it again and keep the receipt.
  ``queryable``   it could duplicate, but something can be asked what happened
                  first. Reconcile, then decide.
  ``unsafe``      it acted and nothing can say whether it landed. Stop, and
                  ask the operator what to do.

**It is not `is_read_only()` under a new name, and the difference is the
point.** That method answers "may this run with nobody watching", which is a
permission question; this field answers "may this be repeated", which is a
recovery one. A tool answers False there and `read_only` here whenever it
reads something the operator wants to approve, and taking a screenshot twice
is not two effects. Reading recovery off the permission answer would have made
half the roster `unsafe` for reasons that have nothing to do with repetition.

**The declaration is refused when it is missing, and the READER still fails
closed.** Both, because they cover different holes. `check_tool_contract`
raises on a kernel tool that declares nothing, so the sweep cannot rot. But
some tools never reach that check at all: an MCP server's tools are registered
after the validation pass, and a tool in the operator's own `<home>/tools/`
loads with a warning rather than being taken away. `behaviour_of` answers
`unsafe` for every one of them, so an undeclared tool is never *treated* as
safe to repeat, whatever the reason it went undeclared.

What this file does NOT own: whether a call may execute at all. That is
`permissions.yaml` and the bash-security checks, and a resumed action is gated
exactly as a fresh one is. Nothing here widens what a tool is permitted to do.
"""

from __future__ import annotations

from typing import Any

#: The answers a tool may give, and the set is closed for the reason
#: `VALID_RECEIPT_KINDS` is: a value no recovery pass knows how to act on is a
#: value that decides nothing.
VALID_RECOVERY_BEHAVIOURS: frozenset[str] = frozenset(
    {
        # Nobody outside can tell whether it ran once or twice. A screenshot
        # under the runtime's own scratch directory counts: the question is
        # whether an EXTERNAL effect repeats, not whether a byte was written.
        "read_only",
        # The same call again is the same effect, not a second one: it
        # converges on a state, or the tool dedupes on a key. A relative nudge
        # to the operator's own view (a zoom step, a scroll) is here too, and
        # deliberately: the field decides whether recovery repeats a call or
        # parks the task, and parking a task to ask about a scroll step the
        # operator can see and reverse is the worse of the two answers.
        "idempotent",
        "queryable",   # ask the far side before deciding
        "unsafe",      # nobody can say whether it landed
    }
)

#: What an undeclared tool is treated as. Never a default that lets recovery
#: act: the whole cost of being wrong here is a duplicated external effect.
UNSAFE = "unsafe"


def behaviour_of(tool: Any) -> str:
    """What recovery may do with this tool, failing closed.

    Read through this rather than off the class, everywhere. A tool that
    escaped the boot check, one whose declaration is a typo, and one from a
    server we do not control all arrive here as `unsafe`, which is the only
    answer that cannot cause a second effect.
    """
    declared = getattr(type(tool), "recovery_behaviour", "")
    return declared if declared in VALID_RECOVERY_BEHAVIOURS else UNSAFE


__all__ = ["UNSAFE", "VALID_RECOVERY_BEHAVIOURS", "behaviour_of"]
