"""credential_list — what accounts the assistant has, and for what.

Names, usernames, purposes, host allowlists, and whether a value is stored.
Never a value. The store's `public()` shape is what this renders, so there is
no field here that could carry one even if this file were rewritten carelessly.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from tesseract.credentials.models import SPEND_STATE_WORDS
from tesseract.credentials.reader import variable_name
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class CredentialListInput(BaseModel):
    pass


def _line(entry: dict[str, Any]) -> list[str]:
    injection = entry["injection"]
    fields = entry["fields"]
    # Every kind the store can hold. A dict missing one raised KeyError and
    # took the whole listing down, which is what happened the day `url` was
    # added for git: the operator's only account made this tool unusable.
    #
    # `env` is the one that names no single place, because every field is
    # handed over at once. It says which variables instead, from the fields
    # themselves, so the code being written does not have to guess at them.
    where = {
        "header": f"header {injection['name']}",
        "query": f"query parameter {injection['name']}",
        "env": (
            "your own code, as "
            + ", ".join(variable_name(f["name"]) for f in fields)
        ),
        "url": f"the web address itself, as {injection['name']}",
    }[injection["kind"]]
    state = SPEND_STATE_WORDS[entry["spend_state"]]

    who = f" as {entry['account']}" if entry["account"] else ""
    lines = [f"{entry['id']} — {entry['service']}{who} — {state}"]
    if entry["purpose"]:
        lines.append(f"    for: {entry['purpose']}")
    if injection["kind"] == "env":
        # No host line. An account read into a command reaches whatever that
        # command reaches, and printing an allowlist beside it would describe
        # a check the runtime does not make for this kind.
        lines.append(f"    given to: {where}, with command_run")
        _name_the_fields(entry, lines)
        return lines
    hosts = ", ".join(entry["allowed_hosts"]) or "no hosts yet, so it cannot be used"
    lines.append(f"    sent to: {hosts}, as {where}")
    _name_the_fields(entry, lines)
    return lines


def _name_the_fields(entry: dict[str, Any], lines: list[str]) -> None:
    """What is filled in and what is still owed, appended to ``lines``.

    Named only when there is more than one, or when one is missing. An account
    holding the single value every account has says nothing extra, because "it
    holds a value called value" is a sentence about the schema rather than
    about the account.
    """
    fields = entry["fields"]
    missing = [f["label"] for f in fields if not f["has_value"]]
    if missing:
        lines.append(
            f"    still needed from the operator: {', '.join(missing)}"
        )
    if len(fields) > 1:
        held = [f["label"] for f in fields if f["has_value"]]
        held_line = f"    holds: {', '.join(held) or 'nothing yet'}"
        if entry["injection"]["kind"] != "env":
            # Which one is SENT is a question only the kinds that send one
            # have. An account read into a command hands over all of them.
            held_line += f"; {entry['injection']['field']} is the one that is sent"
        lines.append(held_line)


class CredentialListTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "your-own-accounts"
    summary: ClassVar[str] = (
        "Your own accounts: service, username, purpose, and whether each is ready."
    )
    use_when: ClassVar[str] = (
        "Use before a task that needs an account of your own, to see whether you "
        "already have one and what it may be used for. The id you get back is "
        "what you name when you ask for something to be done with it."
    )
    not_when: ClassVar[str] = (
        "the operator's own logins, which are theirs and are not listed here. "
        "Reading a value is something no tool does: the runtime puts one into a "
        "request as it is sent, and it never enters this conversation."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    @property
    def name(self) -> str:
        return "credential_list"

    @property
    def input_schema(self) -> type[BaseModel]:
        return CredentialListInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.credentials.store import CredentialStore, CredentialStoreError

        try:
            entries = CredentialStore().list_public()
        except CredentialStoreError as exc:
            return ToolResult(output=f"credential_list: {exc}", is_error=True)

        if not entries:
            return ToolResult(
                output=(
                    "You have no accounts of your own yet. Use credential_request "
                    "to ask the operator for one."
                ),
                metadata={"credentials": []},
            )

        lines: list[str] = []
        for entry in entries:
            lines.extend(_line(entry))
        # Said once, at the end, because "ready" is the word every reader of
        # this list has taken to mean "the service will accept this", and it
        # has never meant that. A live run learned the difference by sending
        # and being refused, then reported that the pipeline needed extending
        # when what it needed was to ask for the rest of the sign-in.
        lines.append(
            "\nReady means a value is stored and may be sent, not that the "
            "service accepts it. An account refused by its service is usually "
            "missing part of its sign-in: ask for the rest by name with "
            "credential_request, giving that account's id."
        )
        return ToolResult(
            output="\n".join(lines),
            metadata={"credentials": entries},
        )
