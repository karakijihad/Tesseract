"""What one request costs, measured once, for every surface that reports it.

Three surfaces reported on the same request and no two agreed. The Conscience
payload panel, the context HUD and the fold arithmetic each counted it their
own way, and after tool namespaces landed none of them was close:

    tools array, wire characters                201,108 chars
    tools array, what the provider charges       10,005 tokens
    a character count at 4 chars per token       50,277  (5.0x high)
    a character count at 3.316                   60,648  (6.1x high)

**A character count cannot measure a tools array at all.** That is not a worse
divisor, it is the wrong instrument. A namespace carries every member's full
schema in the JSON and the provider charges for the header alone, so the
relationship between the bytes and the bill is not a ratio. The head is prose
and divides sensibly; the tools array is structure and does not.

So this module prices the two halves differently and says so:

- **The head**, which is prose, by characters through one divisor.
- **The tools array**, structurally, from the array the adapter actually puts
  on the wire: a loaded schema costs its whole JSON, a namespace costs its
  header, and the member schemas inside a namespace cost nothing.

It returns the parts as well as the total, because the operator sets
`compact_ratio` on these numbers and cannot decide where to cut without seeing
where the tokens are.

**One divisor, and it is `_estimate`'s measured one.** There were three, and
one of them documented a claim that was false: `prompt_payload` divided by four
and said it was "deliberately the same one every adapter's `count_tokens`
uses", which they do not, because they all call `_estimate`. A per-model
divisor declared in `providers.yaml` is the next step and this signature
anticipates it: `chars_per_token` is an argument, defaulted, rather than a
constant read here.

## What is fixed here, and what is still owed

Measured 2026-09-13 on the live registry, 157 tools with 35 in the working set:

    tools array, wire characters                200,672
    a character count at 4 chars per token       50,168
    priced structurally, at the prose divisor    16,492

So the CONTAINMENT error is gone, and it was the large one. What remains is a
divisor error, and this module does not guess at it. Two readings from the
CC-19 probe imply schema JSON runs far leaner than prose:

    whole registry flat   193,444 chars -> 26,659 tokens   7.26 chars/token
    deferring array        54,687 chars -> 10,005 tokens   5.47 chars/token
    prose, regressed over real history                     3.32 chars/token

They disagree with each other, because the second payload is part schema and
part namespace prose, and both were taken on a roster that moved between them.
Neither is a clean measurement of the thing it would be applied to, so neither
becomes a constant here: a number that is not measured and portable does not
get written down. `schema_chars_per_token` is a separate argument, defaulted to
the prose divisor so nothing changes silently, and the measurement that settles
it is one round trip against a live provider with a known payload.

Until then the tools figure reads high, by roughly the ratio above, and it
reads high in the direction that makes a conversation fold EARLY. That is the
safe direction and it is not a reason to leave it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from tesseract.kernel.adapters._estimate import CHARS_PER_TOKEN

log = logging.getLogger(__name__)


def tokens_from_chars(chars: int, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    """Characters to an estimated token count, for PROSE.

    Rounds up, and any non-empty text is worth at least one token: an estimate
    that returns zero for real content reads to every caller as "nothing
    here", and the callers are a fold decision and a context-window guard.

    Not for a tools array. `measure_tools` says why.
    """
    if chars <= 0:
        return 0
    return max(1, int(chars / chars_per_token + 0.5))


@dataclass(frozen=True)
class ToolArraySize:
    """The tools array, priced the way the provider prices it."""

    #: What the provider charges for. The figure every surface should show.
    tokens: int
    #: What the array weighs on the wire. Kept because the gap between this
    #: and `tokens` IS the saving namespacing bought, and a panel that showed
    #: only one of them could not report that the mechanism is working.
    wire_chars: int
    #: Schemas sent in full, and charged in full.
    loaded: int
    #: Namespace headers. Each costs its own description and nothing else.
    namespaces: int
    #: Schemas riding inside a namespace, charged nothing.
    deferred: int

    @property
    def charged_chars(self) -> int:
        """The characters the token figure was derived from."""
        return self._charged_chars

    _charged_chars: int = 0


@dataclass(frozen=True)
class RequestSize:
    """One request, in the two parts it actually divides into."""

    head_chars: int
    head_tokens: int
    tools: ToolArraySize

    @property
    def total_tokens(self) -> int:
        return self.head_tokens + self.tools.tokens


#: Keys the runtime puts on a schema for its own bookkeeping. They never reach
#: a provider, so they never cost anything, and counting them would make the
#: measurement drift every time one is added.
_BOOKKEEPING = frozenset({"defer_loading", "_runtime_search", "group"})


def measure_tools(
    wire_entries: list[dict[str, Any]] | None,
    chars_per_token: float = CHARS_PER_TOKEN,
    schema_chars_per_token: float | None = None,
) -> ToolArraySize:
    """Price the array the adapter is about to send.

    `wire_entries` is what goes on the wire, which for the deferring path is
    `adapters.openai.build_tools_array`'s output. Handed in rather than built
    here on purpose: an array rebuilt for the measurement is an array free to
    disagree with the one the provider charges for, and that disagreement is
    the defect this module exists to remove.

    A namespace is charged for its header and not its members. That is the
    whole difference between 10,005 tokens and 50,277, and it is a structural
    fact rather than a tuning choice: the provider matches a namespace's
    description server-side and appends only the schemas it decides it needs,
    at the END of the context, which is also why the cached prefix survives.

    Prose inside a schema still divides by characters, because a description
    and a parameter name are prose. What does not divide is the containment.

    `schema_chars_per_token` prices the schemas; `chars_per_token` prices the
    namespace headers, which are prose. They are separate arguments because
    they are separate measurements, and the first one has not been taken yet:
    see the module docstring for the two readings that disagree and why
    neither becomes a constant.
    """
    schema_divisor = (
        chars_per_token if schema_chars_per_token is None else schema_chars_per_token
    )
    if not wire_entries:
        return ToolArraySize(
            tokens=0, wire_chars=0, loaded=0, namespaces=0, deferred=0,
        )
    charged = 0
    charged_tokens = 0
    wire = 0
    loaded = 0
    namespaces = 0
    deferred = 0
    for entry in wire_entries:
        try:
            if entry.get("type") == "namespace":
                members = entry.get("tools") or []
                namespaces += 1
                deferred += len(members)
                header = {k: v for k, v in entry.items() if k != "tools"}
                header_chars = len(json.dumps(header))
                charged_tokens += tokens_from_chars(header_chars, chars_per_token)
                charged += header_chars
                wire += len(json.dumps(entry))
                continue
            loaded += 1
            billable = {k: v for k, v in entry.items() if k not in _BOOKKEEPING}
            size = len(json.dumps(billable))
            charged_tokens += tokens_from_chars(size, schema_divisor)
            charged += size
            wire += size
        except (TypeError, ValueError):
            # A schema that will not serialise is one the provider would
            # reject, so it is reported at zero rather than crashing a
            # readout. Loudly, because a tool in that state is a real fault
            # and the number here would be quietly low without it.
            log.warning(
                "request size: a tool entry would not serialise and is "
                "measured as nothing", exc_info=True,
            )
    return ToolArraySize(
        tokens=charged_tokens,
        wire_chars=wire,
        loaded=loaded,
        namespaces=namespaces,
        deferred=deferred,
        _charged_chars=charged,
    )


def measure(
    head: str,
    wire_entries: list[dict[str, Any]] | None,
    chars_per_token: float = CHARS_PER_TOKEN,
    schema_chars_per_token: float | None = None,
) -> RequestSize:
    """One request's cost, head and tools, in one call.

    This is what every surface asks. A surface that added the two halves its
    own way would be a fourth answer to a question that already had three that
    disagreed.
    """
    chars = len(head or "")
    return RequestSize(
        head_chars=chars,
        head_tokens=tokens_from_chars(chars, chars_per_token),
        tools=measure_tools(wire_entries, chars_per_token, schema_chars_per_token),
    )


def wire_entries_for(
    registry: Any,
    enabled_extended: set[str] | None,
    core_names: frozenset[str] | None = None,
) -> list[dict]:
    """The tools array a turn would send, from a registry.

    The deferring projection, because that is what the chat brain runs on and
    what every measured figure in this module was taken against. A caller
    holding a live adapter should project through it instead; this is for the
    readouts that have a registry and no request in flight.

    `core_names` forwards straight to `schemas_for_adapter`, and only when
    given: `None` (the default) omits the keyword entirely rather than
    passing it as `None`, so a `registry` double written before this
    parameter existed (a bare `enabled_extended`-only signature) keeps
    working exactly as it did. `None` also reads the registry's live `tier`,
    which is right for a caller with no conversation to protect — a panel, a
    capability readout. A caller sizing a running conversation's actual
    request passes its held snapshot (`ChatSession._core_tools_for_turn`), or
    this measures an array the turn is not the one that will be sent — the
    two diverge the moment an ambient working-set change lands between the
    head freeze and the size read.

    Returns an empty list rather than raising: a panel that cannot size the
    tools array should say zero and keep its head figure, not fail.
    """
    if registry is None:
        return []
    try:
        from tesseract.kernel.adapters.openai import build_tools_array

        kwargs: dict[str, Any] = {
            "enabled_extended": enabled_extended if enabled_extended is not None else set(),
        }
        if core_names is not None:
            kwargs["core_names"] = core_names
        classified = registry.schemas_for_adapter(**kwargs)
        return build_tools_array(classified)
    except Exception:
        log.exception("request size: could not build the tools array to measure")
        return []


def declared_schema_divisor(fields: Any, where: str) -> float | None:
    """A catalog entry's `schema_chars_per_token`, or `None` when it has none.

    Absent is a statement, not a gap to fill: the model has not been measured,
    and the reader then prices schemas as prose and says so. Present and not a
    positive number raises, because a wrong figure here is a wrong boundary.
    """
    raw = (fields or {}).get("schema_chars_per_token")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ValueError(
            f"{where}: schema_chars_per_token must be a positive number, got {raw!r}"
        )
    return float(raw)


def chat_brain_schema_divisor() -> float | None:
    """What the chat brain's first model declares, for readers with no session.

    The payload panel and the Conscience route price a turn without holding a
    `ChatSession`, so they ask the catalog for the model a turn would ride.
    `None` when the catalog cannot say, which prices schemas as prose.

    A figure that is not a number is logged as an error and read as `None`
    rather than raised: these readers build every surface's reading at once,
    and one bad catalog value must cost the tools figure its precision, not
    take every reading down with it. Boot still refuses the same value.
    """
    try:
        from tesseract.brain.boot import load_bundle

        role = load_bundle().roles.get("chat_brain")
    except Exception:
        log.warning("request size: the catalog would not load", exc_info=True)
        return None
    ref = getattr(role, "primary", None)
    if ref is None:
        return None
    try:
        return declared_schema_divisor(ref.model.fields, f"providers.yaml entry for {ref.ref}")
    except ValueError:
        log.error("request size: pricing schemas as prose", exc_info=True)
        return None


__all__ = [
    "RequestSize",
    "ToolArraySize",
    "chat_brain_schema_divisor",
    "declared_schema_divisor",
    "measure",
    "measure_tools",
    "tokens_from_chars",
    "wire_entries_for",
]
