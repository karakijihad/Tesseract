"""What a tool leaves behind, in a shape a later pass can read.

A run's only account of what it did in the world used to be the model's own
sentence about it. That is an artifact, never a verdict: nothing on disk could
answer whether a message was actually sent, whether a commit actually landed,
or whether the file that was reported written exists.

A receipt is the smallest thing that fixes it. Three fields, and each earns its
place by answering a different question:

  ``kind``     what sort of thing this is, from a closed set
  ``id``       the FAR SIDE's own identifier, never one minted here. A
               Telegram message id, a commit hash, a content hash. It is what
               makes the claim checkable by somebody who does not trust us.
  ``locator``  where to go and look. A path, a repository, a chat.

**It carries no content, deliberately.** A receipt says a thing exists and
where; reading it is the reader's business. A receipt that copied what it
points at would be a second copy of the operator's data, kept for a purpose
that never needs to read it, which is the reasoning `brain/tool_usage.py`
already applies to the usage ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The kinds a receipt may declare, and the set is closed for the same reason
#: `RunOutcome` is: a kind nothing can render is a kind nobody can act on.
#:
#: `none` is a real answer and not an absence. It means the call had nothing to
#: point at and the tool says so, which is different from a tool that was
#: supposed to answer and did not. That difference is the whole of
#: `RunOutcome.UNVERIFIED`.
VALID_RECEIPT_KINDS: frozenset[str] = frozenset(
    {
        "message",  # something said on a channel
        "commit",   # a commit in a repository
        "file",     # a file on disk, identified by its content
        "record",   # a record in one of the app's own stores
        "job",      # a scheduled row
        "none",     # nothing to point at, and the tool says so
    }
)

#: What a tool declares when it can never leave a mark. Read off the CLASS.
NO_RECEIPT = "none"


@dataclass(frozen=True)
class Receipt:
    """One mark a call left, and where to find it."""

    kind: str
    id: str = ""
    locator: str = ""

    def __post_init__(self) -> None:
        if self.kind not in VALID_RECEIPT_KINDS:
            raise ValueError(
                f"receipt kind {self.kind!r} is not one of "
                f"{sorted(VALID_RECEIPT_KINDS)}. The set is closed: a kind "
                "nothing can render is a kind nobody can act on."
            )

    @classmethod
    def nothing(cls) -> "Receipt":
        """This call had no external effect, and that is the answer.

        For a tool that CAN leave a mark and did not on this call: `git status`
        against `git commit`, a dry run against a real one. Without it, every
        read through a tool that can also write would be recorded as work
        nobody can verify."""
        return cls(kind=NO_RECEIPT)

    @property
    def points_at_something(self) -> bool:
        return self.kind != NO_RECEIPT

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "locator": self.locator}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Receipt":
        return cls(
            kind=str(raw.get("kind") or NO_RECEIPT),
            id=str(raw.get("id") or ""),
            locator=str(raw.get("locator") or ""),
        )


__all__ = ["NO_RECEIPT", "VALID_RECEIPT_KINDS", "Receipt"]
