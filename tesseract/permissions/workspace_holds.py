"""Which of the operator's own documents the assistant must ask about.

One posture for all six was too blunt. `OPERATING.md` is the rules the
assistant works by and `DIARY.md` is a line it adds to its day, and under a
mode that says `auto` the block could not tell them apart: it named a posture
per mode and nothing per document.

This is the writer for the exception list. It changes which documents are held
back and nothing else. What the two answers MEAN, and the fact that no raw
write verb reaches any of these files whatever is chosen here, stay in
`permissions.yaml` and in `policy.py`, for the reason the retention table
keeps `may_delete` out of the operator's hands: a rule that can be edited into
nothing is not a rule.

There is one writer because there is one decision. The Settings panel, the
same words typed in the cockpit and the same words said on a phone all send
`workspace_hold`, which ends up here. A second door would be a second answer
to a question this file exists to have one answer to.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from tesseract.lib.yaml_io import round_trip_yaml

#: Reading the file and writing it back are one act. Two round trips racing
#: each other put every key back as each of them found it, and the one that
#: lost leaves an operator looking at a document they held back that is not.
_writing = threading.Lock()


class HoldError(ValueError):
    """A hold that cannot be set, in the words the operator should read."""


def set_document_hold(
    config_dir: Path, document: str, must_ask: bool
) -> tuple[bool, bool]:
    """Hold one document back, or let it follow the mode again.

    Returns what it was and what it now is, both as "the assistant must ask
    first". Reports rather than repairs: a block that does not load is said to
    be that, because a control that half-fixes a file leaves a machine whose
    next boot refuses it.

    The comments in `permissions.yaml` are what explains the block to whoever
    opens it, so this is a round trip and not a dump.
    """
    from tesseract.permissions.policy import (
        _OVERRIDES_KEY,
        _document_name,
        load_permission_policy,
    )

    name = _document_name(document)
    path = config_dir / "permissions.yaml"
    with _writing:
        policy = load_permission_policy(path)
        documents = policy.workspace_documents
        if name not in documents:
            raise HoldError(
                f"{name} is not one of the operator's documents, so nothing "
                f"was changed. They are: {', '.join(documents)}"
            )
        was = name in policy.workspace_document_holds

        def _apply(doc: Any) -> None:
            proposal = doc["workspace_documents"]["proposal"]
            if must_ask:
                proposal.setdefault(_OVERRIDES_KEY, {})[name] = "ask"
                return
            holds = proposal.get(_OVERRIDES_KEY)
            if holds is None or name not in holds:
                return
            survivor = _last_key_other_than(holds, name)
            if survivor is not None:
                _move_trailing_comment(holds, name, holds, survivor)
                holds.pop(name)
                return
            # It was the only one held back, so the whole map goes: an empty
            # map left behind reads as a list somebody emptied, and the key
            # going away says what is true. Both the doomed entry's trailing
            # comment and the map's own have to land on what now precedes
            # them, which is the last mode `proposal` names.
            above = _last_key_other_than(proposal, _OVERRIDES_KEY)
            _move_trailing_comment(holds, name, proposal, above)
            _move_trailing_comment(proposal, _OVERRIDES_KEY, proposal, above)
            holds.pop(name)
            del proposal[_OVERRIDES_KEY]

        if was != must_ask:
            round_trip_yaml(path, _apply)

        after = load_permission_policy(path)
        return was, name in after.workspace_document_holds


#: Where ruamel keeps the comment that FOLLOWS a key's value, in the four-slot
#: entry it stores per key. Named rather than written as `[2]` at two call
#: sites, because the number says nothing about what is being moved.
_AFTER_THE_VALUE = 2


def _last_key_other_than(mapping: Any, doomed: Any) -> Any:
    """The key that will be last once `doomed` is gone, or None if none is."""
    survivors = [k for k in mapping if k != doomed]
    return survivors[-1] if survivors else None


def _move_trailing_comment(
    source: Any, doomed: Any, target: Any, target_key: Any
) -> None:
    """Carry the comment that trails `doomed` onto the key that outlives it.

    ruamel attaches a comment to the key it FOLLOWS, so the block explaining
    the next section of the file hangs off the last entry of this one. Popping
    that entry takes the block with it, silently: measured on the live file,
    letting `CHANNEL.md` go deleted thirty-five lines describing the read-only
    bash allowlist, and nothing in the write path or the tests noticed.

    Best effort by construction. A round trip that cannot find a home for a
    comment must still make the change the operator asked for; losing a
    comment is the bug, and refusing the change over one would be a worse one.
    """
    if target_key is None:
        return
    slot = getattr(source, "ca", None)
    entry = None if slot is None else slot.items.get(doomed)
    token = None if entry is None else entry[_AFTER_THE_VALUE]
    if token is None:
        return
    home = getattr(target, "ca", None)
    if home is None:
        return
    kept = home.items.setdefault(target_key, [None, None, None, None])
    if kept[_AFTER_THE_VALUE] is None:
        kept[_AFTER_THE_VALUE] = token
        return
    # The survivor already ends in a comment, so both belong to it and in this
    # order. Only reachable if a comment sat BETWEEN two entries, which the
    # live file has never had; joined rather than dropped so that stays true.
    kept[_AFTER_THE_VALUE].value = "\n".join(
        (kept[_AFTER_THE_VALUE].value.rstrip("\n"), token.value.lstrip("\n"))
    )


__all__ = ["HoldError", "set_document_hold"]
