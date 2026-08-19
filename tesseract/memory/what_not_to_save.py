"""WHAT_NOT_TO_SAVE exclusion policy.

The exclusion rules are the pattern tuples below and nothing else. This module
used to also parse `memory-store/WHAT_NOT_TO_SAVE.md` into a `categories` list
that no code ever read, and warn when the file was absent — a warning about a
parse whose result was discarded, on a file the loader's own root never held.
The markdown is documentation for the operator; it configures nothing.

Provides a should_save() check that runs before every memory write.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_CODE_PATTERNS = [
    re.compile(r"^\s*(def |class |import |from .+ import |@\w+)", re.MULTILINE),
    re.compile(r"^\s*(if __name__|try:|except |raise )", re.MULTILINE),
    re.compile(r"\b(return |yield |async def |await )\b"),
]

_GIT_PATTERNS = [
    re.compile(r"\bgit (log|blame|diff|show|commit)\b", re.IGNORECASE),
    re.compile(r"\bcommit [0-9a-f]{7,40}\b", re.IGNORECASE),
]

_EPHEMERAL_PATTERNS = [
    re.compile(r"\b(currently working on|in-progress|temporary|right now I'm)\b", re.IGNORECASE),
]

# The assistant's own standing instructions, whichever surface carries them:
# the loaded rule set, or an agent-instruction file a CLI dropped in the tree.
_INSTRUCTION_ECHO_PATTERNS = [
    re.compile(
        r"\b(CLAUDE|AGENTS)\.md (says|specifies|defines|instructs)\b", re.IGNORECASE
    ),
    re.compile(r"\bthe (CLAUDE|AGENTS)\.md\b", re.IGNORECASE),
    re.compile(
        r"\bmy (standing )?(instructions|system prompt|rules) "
        r"(say|state|specify|require)\b",
        re.IGNORECASE,
    ),
]

_ROUTINE_PATTERNS = [
    re.compile(r"^(hello|hi|hey|thanks|thank you|ok|okay|sure|got it)[\.\!\s]*$", re.IGNORECASE),
]

# F1-2026-04-20: three new groups targeting the dominant junk template
# observed in the pre-wipe memory-store (59 user/ mems, zero blocked).
_REQUEST_ECHO_PATTERNS = [
    re.compile(r"^(ok|sure|understood|got it|noted)[,\.]?\s", re.IGNORECASE),
    re.compile(r"^(you|the (operator|user)) (asked|requested|wanted|said|told|want(ed)? me)\b", re.IGNORECASE),
    re.compile(r"^(as (you|the operator) (requested|asked))\b", re.IGNORECASE),
    re.compile(r"^user[_ ](asked|requested|sent|wanted|said)\b", re.IGNORECASE),
]

_TURN_SUMMARY_PATTERNS = [
    re.compile(r"^(in )?this turn[,\s]", re.IGNORECASE),
    re.compile(r"^summary of (this|the) turn\b", re.IGNORECASE),
    re.compile(r"^(what i did|what was done) (in|this) (turn|session)\b", re.IGNORECASE),
    re.compile(r"^turn (summary|recap)\b", re.IGNORECASE),
    re.compile(r"^last[_ ](action|request|query|question|interaction|read|user[_ ]question)\b", re.IGNORECASE),
    re.compile(r"^recent (user query|delegate_\w+ attempts)\b", re.IGNORECASE),
]

# Body length below this is a "trivial body" — the title is doing all the work,
# and the body is almost always an echo of the title.
_TRIVIAL_BODY_MIN_CHARS = 80


class WhatNotToSave:
    def __init__(self) -> None:
        # Set by should_save() on the last call — read by MemoryStore.write()
        # so the forensic writes.jsonl log carries the specific reason.
        self.last_reason: str | None = None

    def should_save(self, content: str) -> bool:
        # `last_reason` names the specific rule that blocked, so callers can
        # log forensics — `store.py` writes it to events/writes.jsonl.
        self.last_reason = None

        for pattern in _CODE_PATTERNS:
            if pattern.search(content):
                self.last_reason = "code_pattern"
                logger.debug("Blocked by code pattern exclusion")
                return False

        for pattern in _GIT_PATTERNS:
            if pattern.search(content):
                self.last_reason = "git_history"
                logger.debug("Blocked by git history exclusion")
                return False

        for pattern in _EPHEMERAL_PATTERNS:
            if pattern.search(content):
                self.last_reason = "ephemeral_task_state"
                logger.debug("Blocked by ephemeral task state exclusion")
                return False

        for pattern in _INSTRUCTION_ECHO_PATTERNS:
            if pattern.search(content):
                self.last_reason = "instruction_echo"
                logger.debug("Blocked by standing-instruction echo exclusion")
                return False

        for pattern in _ROUTINE_PATTERNS:
            if pattern.search(content):
                self.last_reason = "routine_ack"
                logger.debug("Blocked by routine session details exclusion")
                return False

        for pattern in _REQUEST_ECHO_PATTERNS:
            if pattern.search(content):
                self.last_reason = "request_echo"
                logger.debug("Blocked by request-echo exclusion")
                return False

        for pattern in _TURN_SUMMARY_PATTERNS:
            if pattern.search(content):
                self.last_reason = "turn_summary"
                logger.debug("Blocked by turn-summary exclusion")
                return False

        if len(content.strip()) < _TRIVIAL_BODY_MIN_CHARS:
            self.last_reason = "trivial_body"
            logger.debug("Blocked by trivial-body exclusion (%d chars)", len(content.strip()))
            return False

        return True
