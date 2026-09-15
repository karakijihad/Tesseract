"""A tool result too long to send is saved whole, and the model gets the path.

A turn can outgrow the request: one tool reads something large, and the next
payload is bigger than a model will take. The old answers both lost content. The
per-result ceiling cut the result, and the emergency guard cleared tool results
already sitting in history, which is also the part of the prompt the provider has
cached, so every firing paid for a full re-read on the next turn as well.

What this does instead: the whole result is written to a file under
`runtime/tool-results/<day>/`, and the model receives the original size, the
path, and the opening characters. The read tools accept an absolute path, so the
rest is one `file_read` away.

**The invariant everything here turns on: a result is decided once, before it
enters history, and never again.** A result the provider has seen is part of a
cached prefix, and replacing it afterwards is a cache miss on every turn that
follows. So nothing in this module is ever handed history. `fit_one` runs on a
result as it arrives; `fit_batch` runs on one turn's batch before it is appended,
and the indices already appended arrive as `sent` and are counted but never
chosen. There is no set of ids to consult later because there is no later.

The properties that have to hold together, and that any change here is checked
against as a list rather than one at a time:

1. Nothing is lost. The file is written in full before a preview replaces the
   text, and a write that fails leaves the result as it was.
2. Nothing already sent is touched (the invariant above).
3. A batch of many results is bounded as well as one large result:
   `per_message_chars` across a turn, `max_chars` per result.
4. A tool whose output would be read back with itself can opt out, by declaring
   `max_result_chars = math.inf`. `file_read` does. What bounds such a tool is
   the window ceiling in `chat.bound_tool_result`, and the source is still on
   disk for a narrower read.
5. The files are reaped: `retention.yaml::tool_results`.
6. The request still fits once all of the above are honoured. A preview is far
   smaller than the ceiling that triggered it, which `load_tool_result_spill`
   enforces.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Collection

from tesseract.config.runtime_limits import (
    ToolResultSpill,
    default_runtime_config_path,
    load_tool_result_spill,
)
from tesseract.lib import clock

logger = logging.getLogger(__name__)

SPILL_OPEN = "<persisted-output>"
SPILL_CLOSE = "</persisted-output>"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")

_LIMITS: ToolResultSpill | None = None
_LIMITS_READ = False


def spill_root() -> Path:
    """`runtime/tool-results/`: machine-local, dated directories beneath it."""
    from tesseract.paths import runtime_dir

    return runtime_dir() / "tool-results"


def limits() -> ToolResultSpill | None:
    """The three limits, read once per process. `None` turns saving off.

    Degrades the way `chat._result_window_share` does and for its reason: this
    is protection, and a runtime that cannot read its config should lose the
    protection and say so rather than lose the ability to call tools. The
    window ceiling behind it still holds.
    """
    global _LIMITS, _LIMITS_READ
    if not _LIMITS_READ:
        try:
            _LIMITS = load_tool_result_spill(default_runtime_config_path())
        except (OSError, ValueError) as exc:
            logger.warning(
                "oversized tool results will not be saved to disk: %s. They are "
                "cut at the window ceiling instead, and what is cut is lost.",
                exc,
            )
            _LIMITS = None
        _LIMITS_READ = True
    return _LIMITS


def ceiling_for(tool: Any, spill: ToolResultSpill) -> float:
    """How long one result from `tool` may be before it is saved instead.

    Read off the class, never the instance: a test registry hands back mocks,
    and an instance attribute on one of those is a mock rather than a number. A
    tool may declare lower than the runtime and never higher, except for the
    explicit opt-out.
    """
    declared = getattr(type(tool), "max_result_chars", None) if tool is not None else None
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return spill.max_chars
    if declared == math.inf:
        return math.inf
    return min(declared, spill.max_chars)


def fit_one(
    result: Any,
    *,
    tool: Any,
    tool_name: str,
    call_id: str,
    spill: ToolResultSpill | None,
) -> Any:
    """One result, as it arrives. Over its ceiling it is saved and previewed."""
    if spill is None or len(result.output) <= ceiling_for(tool, spill):
        return result
    return _save(result, tool_name, call_id, spill)


def fit_batch(
    results: dict[int, tuple[Any, Any]],
    *,
    sent: Collection[int],
    tool_of: Callable[[str], Any],
    spill: ToolResultSpill | None,
) -> dict[int, tuple[Any, Any]]:
    """One turn's results, bounded together before they are appended.

    `results` maps a call's index to `(tool call, result)`. Everything counts
    toward the total, `sent` included, because it is all in the same message;
    only what is not in `sent` may be chosen. The largest go first, so the
    fewest results lose their inline text. If what was already sent is over on
    its own, the overage is accepted rather than reached for.
    """
    if spill is None:
        return results
    total = sum(len(result.output) for _call, result in results.values())
    if total <= spill.per_message_chars:
        return results
    eligible = sorted(
        (
            index
            for index, (call, result) in results.items()
            if index not in sent
            and not result.output.startswith(SPILL_OPEN)
            and ceiling_for(tool_of(call.name), spill) != math.inf
        ),
        key=lambda index: len(results[index][1].output),
        reverse=True,
    )
    fitted = dict(results)
    for index in eligible:
        if total <= spill.per_message_chars:
            break
        call, result = fitted[index]
        saved = _save(result, call.name, call.id, spill)
        total -= len(result.output) - len(saved.output)
        fitted[index] = (call, saved)
    return fitted


def _save(result: Any, tool_name: str, call_id: str, spill: ToolResultSpill) -> Any:
    """Save `result` and return it previewed, or return it unchanged.

    Unchanged when the preview would be no shorter than the result. The header
    and the path cost characters of their own, so a result only a little over
    the preview length is shortest left as it is, and a batch that saved it
    would grow rather than shrink.
    """
    path = _new_path(call_id)
    block = _preview_block(result.output, tool_name, path, spill.preview_chars)
    if len(block) >= len(result.output):
        return result
    try:
        _write(path, result.output)
    except OSError as exc:
        logger.error(
            "could not save the %d-character result of %s to disk (%s). It "
            "goes on whole, and only the window ceiling bounds it.",
            len(result.output), tool_name, exc,
        )
        return result
    return dataclasses.replace(result, output=block)


def _new_path(call_id: str) -> Path:
    stem = _UNSAFE_NAME.sub("_", call_id)[:80] or "call"
    return spill_root() / clock.today().isoformat() / f"{stem}-{uuid.uuid4().hex[:8]}.txt"


def _write(path: Path, output: str) -> None:
    """Write the whole text to a new file. Never overwrites one.

    `newline=""` so Windows does not turn every newline into two characters and
    make the file disagree with the size the preview reports.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", errors="surrogatepass", newline="") as fh:
        fh.write(output)


def _preview_block(output: str, tool_name: str, path: Path, preview_chars: int) -> str:
    head = output[:preview_chars]
    # End on a line where there is one near the limit, so the preview does not
    # stop mid-word and read as corruption.
    newline = head.rfind("\n")
    if newline >= preview_chars // 2:
        head = head[:newline]
    more = "\n..." if len(head) < len(output) else ""
    return (
        f"{SPILL_OPEN}\n"
        f"{tool_name} returned {len(output):,} characters, too many to send in "
        f"one piece. All of it is saved at {path}. Read it with file_read, a "
        f"range of lines at a time.\n\n"
        f"The first {len(head):,} characters:\n{head}{more}\n"
        f"{SPILL_CLOSE}"
    )
