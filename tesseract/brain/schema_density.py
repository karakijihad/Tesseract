"""What a model's provider charges for tool schemas, measured and kept true.

`providers.yaml::schema_chars_per_token` says how many characters of tool schema
JSON this model's provider charges one token for. Schema JSON does not tokenize
like prose, so every surface that prices a request reads this figure rather
than the prose divisor (`request_size`). This module is where the figure comes
from, and the nightly provider check is what calls it.

The properties that have to hold together:

1. **Measured the way it is read.** The characters are what `request_size`
   counts for the loaded schemas; the tokens are what this model's own provider
   reports for sending those same schemas through its own adapter. Two requests,
   the same one-line input with and without the schemas.
2. **A missing figure is written, a present one is only checked.** A model
   somebody added has no figure and nothing to be careful about, so the reading
   goes into the catalog. A figure that is there was put there on purpose, so a
   reading that disagrees beyond the tolerance raises a card and writes nothing.
3. **A reading that cannot be taken changes nothing.** It is recorded as not
   measured, with the reason.
4. **Rechecked on a clock, not every night.** A declared figure is measured
   again only once its last reading is older than the recheck window.
5. **Every call is metered**, because the adapter it is handed is.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from tesseract.brain import request_size
from tesseract.kernel.adapters.base import ChunkType

log = logging.getLogger(__name__)

_PROMPT = "Reply with the single word OK."

WRITTEN = "written"
AGREES = "agrees"
DISAGREES = "disagrees"
KEPT = "kept"
UNMEASURED = "unmeasured"


@dataclass(frozen=True)
class Reading:
    ref: str
    chars: int
    tokens: int

    @property
    def chars_per_token(self) -> float:
        return round(self.chars / self.tokens, 2)


def loaded_schemas(registry: Any) -> list[dict[str, Any]]:
    """The schemas a turn sends in full: the working set, nothing deferred."""
    classified = registry.schemas_for_adapter(enabled_extended=set())
    return [t for t in classified if not t.get("defer_loading")]


async def _input_tokens(adapter: Any, options: Any, tools: list | None) -> int:
    usage: dict | None = None
    async for chunk in adapter.stream(
        [{"role": "user", "content": _PROMPT}], tools=tools, options=options
    ):
        if chunk.type == ChunkType.ERROR:
            raise RuntimeError(chunk.error or "the provider answered with an error")
        if chunk.type == ChunkType.STOP and isinstance(chunk.raw, dict):
            usage = chunk.raw.get("usage")
    tokens = int((usage or {}).get("input_tokens") or 0)
    if tokens <= 0:
        raise RuntimeError("the provider reported no input tokens")
    return tokens


async def measure(ref: str, adapter: Any, options: Any, schemas: list[dict]) -> Reading:
    """One reading for one model. Raises when there is nothing to divide."""
    from tesseract.kernel.adapters.openai import build_tools_array

    # Counted from the adapter's own projection, which is what goes on the wire:
    # a provider that defers loading brings its own search, so the runtime's
    # `tool_search` is dropped from the request and must not be in the count.
    projected = adapter.project_tools(schemas) or []
    chars = request_size.measure_tools(build_tools_array(projected)).charged_chars
    bare, loaded = await asyncio.gather(
        _input_tokens(adapter, options, None),
        _input_tokens(adapter, options, schemas),
    )
    tokens = loaded - bare
    if chars <= 0 or tokens <= 0:
        raise RuntimeError(f"nothing to divide: {chars} characters, {tokens} tokens")
    return Reading(ref=ref, chars=chars, tokens=tokens)


def judge(declared: float | None, measured: float, tolerance: float) -> str:
    if declared is None:
        return WRITTEN
    return AGREES if abs(measured - declared) <= declared * tolerance else DISAGREES


def write_declared(ref: str, value: float, catalog: Path) -> bool:
    """Put `value` on the entry, unless something already did. True if written.

    Checked again inside the write, so a figure the operator added while the
    reading was being taken is never replaced by it.
    """
    from tesseract.lib.yaml_io import round_trip_yaml

    tier, provider, model = ref.split(".", 2)
    wrote = False

    def _set(doc: Any) -> None:
        nonlocal wrote
        entry = doc[tier][provider]["models"][model]
        if entry.get("schema_chars_per_token") is None:
            entry["schema_chars_per_token"] = value
            wrote = True

    round_trip_yaml(catalog, _set)
    return wrote


def record_path() -> Path:
    from tesseract.paths import log_dir

    return log_dir("provider-health") / "schema-density.jsonl"


def _last_measured(ref: str) -> datetime | None:
    path = record_path()
    if not path.is_file():
        return None
    last: datetime | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("ref") != ref or row.get("outcome") == UNMEASURED:
            continue
        try:
            last = datetime.fromisoformat(row["at_utc"])
        except (KeyError, ValueError):
            continue
    return last


def _record(rows: list[dict[str, Any]]) -> None:
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


async def verify(
    targets: list[tuple[str, str]],
    *,
    registry: Any,
    entry_builder: Callable[..., Any],
    cost_ledger: Any,
    tolerance: float,
    recheck_days: float,
    catalog: Path,
    publish: Callable[[dict[str, Any]], None],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Measure what is due, write what is missing, raise a card on a disagreement.

    `targets` is `(ref, billing role)` for each `api` chat model that just
    answered its health probe.
    """
    from tesseract.brain.boot import load_bundle

    moment = now or datetime.now(timezone.utc)
    bundle = load_bundle()
    schemas = loaded_schemas(registry)
    due: list[tuple[str, str, float | None]] = []
    for ref, billing in targets:
        declared = request_size.declared_schema_divisor(
            bundle.resolve(ref).model.fields, f"providers.yaml entry for {ref}"
        )
        last = _last_measured(ref)
        if declared is not None and last is not None and moment - last < timedelta(days=recheck_days):
            continue
        due.append((ref, billing, declared))

    async def _one(ref: str, billing: str) -> Reading:
        adapter, options = entry_builder(
            ref, billing_key=billing, log_label="schema_density", cost_ledger=cost_ledger
        )[0]
        return await measure(ref, adapter, options, schemas)

    readings = await asyncio.gather(
        *(_one(ref, billing) for ref, billing, _ in due), return_exceptions=True
    )
    rows: list[dict[str, Any]] = []
    for (ref, _billing, declared), reading in zip(due, readings):
        row: dict[str, Any] = {"at_utc": moment.isoformat(), "ref": ref, "declared": declared}
        if isinstance(reading, BaseException):
            log.warning("schema density: %s could not be measured: %s", ref, reading)
            rows.append({**row, "outcome": UNMEASURED, "reason": str(reading)[:200]})
            continue
        measured = reading.chars_per_token
        outcome = judge(declared, measured, tolerance)
        if outcome == WRITTEN and not write_declared(ref, measured, catalog):
            outcome = KEPT
        rows.append({
            **row, "outcome": outcome, "measured": measured,
            "chars": reading.chars, "tokens": reading.tokens,
        })
    if rows:
        _record(rows)
    disagreements = [r for r in rows if r["outcome"] == DISAGREES]
    if disagreements:
        publish({"schema_density": disagreements})
    return rows


__all__ = [
    "AGREES",
    "DISAGREES",
    "KEPT",
    "Reading",
    "UNMEASURED",
    "WRITTEN",
    "judge",
    "loaded_schemas",
    "measure",
    "record_path",
    "verify",
    "write_declared",
]
