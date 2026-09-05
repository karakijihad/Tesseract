"""Why a provider call failed, and whose fault the record says it was.

Two questions the runtime spent a year answering with one string:

- **Could we ask?** A binary that is not on PATH, a probe that timed out, a
  spawn that raised. Nothing about the provider is known. This is OURS.
- **What did the provider say?** A refused subscription, a spent quota, a
  gateway that fell over. This is THEIRS, and their own words are the record.

Collapsing both into one `reason` field is what made a whole day
unfalsifiable: the nightly report read `cli.codex.gpt56_terra unavailable`
with `evidence.reason: "binary not found on PATH"`, the operator read it as
"the account is out of usage", and neither reading could be checked against
the other because the record did not say which question had been answered.

**The classifier maps text to a kind. It never authors the reason.** A kind is
this runtime's word for a shape of failure, chosen from a closed list; the
reason is the provider's sentence, kept verbatim and bounded. When no rule
matches, the kind is `unknown` and the provider's sentence is still kept ---
an honest "we did not recognise this" beside the text we could not read is
worth more than a confident wrong bucket.

Not to be confused with `provider_health.py::provider_drift_kind`, which
answers a narrower question over lane-turn error text --- *is this about the
provider at all* --- and deliberately refuses a bare "quota" because a
delegate that fills a disk says it too. That narrowness is a feature there and
would be a bug here, so the two stay separate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# A provider's refusal is one sentence and a stack trace is not. The cap is
# per fault rather than per report so a chatty CLI cannot crowd out the other
# providers' answers.
MAX_FAULT_CHARS = 400

# Every kind this module can put in a `ProviderFault.kind`, and the list is
# closed so a reader downstream can be TOTAL over it. The first five come from
# `classify`, reading the provider's own words; the last two are what we
# observed when there were no words to read. They were already in the field
# before they were in this list, which is what made the list unenforceable:
# a table over `FaultKind` looked complete and had two holes in it.
FaultKind = Literal[
    # Read out of the provider's own words, or out of the status code it sent
    # them with. `CLASSIFIED_KINDS` below is exactly this half.
    "usage",
    "auth",
    "not_found",
    "rate_limit",
    "schema",
    "server",
    "unknown",
    # Observed by us, because there were no words to read: a call that never
    # answered, one that answered with nothing, one whose answer was the wrong
    # shape, and a check that could not run at all. Every one of these was
    # already being put in the field by a caller somewhere in the tree while
    # the list above claimed to be closed --- which is what made the list
    # unenforceable: a table over `FaultKind` looked total and had four holes.
    "no_answer",
    "empty_answer",
    "shape_mismatch",
    "check_failed",
]

#: The subset of `FaultKind` that `classify` can return --- everything a
#: provider's own words (or its status code) can be read as. `no_answer` and
#: `check_failed` are the other two: they record what WE observed when there
#: was nothing to read, so no rule produces them and no reader of provider
#: text has to handle them. A table indexed by a classified kind is total over
#: this; a table over recorded faults is total over `FaultKind`.
CLASSIFIED_KINDS: tuple[FaultKind, ...] = (
    "usage",
    "auth",
    "not_found",
    "rate_limit",
    "schema",
    "server",
    "unknown",
)

# Which of the two cooldown windows a fault takes. Lives here, beside
# `FaultKind`, because "does this kind clear on its own" is a fact about the
# KIND and now has three readers: the chain's per-entry breaker, the drift
# record, and the tool funnel's breaker. It was written in `adapter_chain`
# when the chain was the only one that asked.
NEEDS_A_PERSON = "needs_a_person"
SELF_CLEARING = "self_clearing"

_WINDOW_CLASS: dict[str, str] = {
    # Nothing this entry does changes these. A person does.
    "usage": NEEDS_A_PERSON,
    "auth": NEEDS_A_PERSON,
    "not_found": NEEDS_A_PERSON,
    # These may have cleared by the next turn.
    "rate_limit": SELF_CLEARING,
    "server": SELF_CLEARING,
    "unknown": SELF_CLEARING,
    "no_answer": SELF_CLEARING,
    "empty_answer": SELF_CLEARING,
    "shape_mismatch": SELF_CLEARING,
    # About what we SENT, not about this entry. See the note above.
    "schema": SELF_CLEARING,
    # We never got as far as asking, so we know nothing about this entry and
    # holding it shut for an hour would be a claim we cannot support.
    "check_failed": SELF_CLEARING,
}


def window_class_for_fault(kind: str) -> str:
    """Which of the two cooldown windows a fault of this kind takes.

    Raises on a kind nobody placed --- see `_WINDOW_CLASS`.
    """
    return _WINDOW_CLASS[kind]


# Says a cut happened, rather than leaving the operator to wonder why a
# sentence starts mid-word.
_ELLIPSIS = "... "

# The metadata key a tool sets on a result that failed BEFORE anything left
# this machine. A probe reading that tool's results cannot otherwise tell a
# missing API key from a provider that refused, and was recording both as the
# provider's fault.
FAULT_ORIGIN_KEY = "fault_origin"
FAULT_OURS = "ours"


# A bare three-digit number proves nothing. The text searched here is a CLI's
# stderr --- byte counts, token counts, durations, line numbers --- so
# `\b5\d\d\b` on its own matches "read 512 bytes" and turns an unrecognised
# failure into a server outage. A status code has to look like one: the word
# in front of it is what makes it a claim about HTTP rather than a number.
def _status(codes: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?:https?|status|code|error|response|returned)\D{{0,12}}\b(?:{codes})\b",
        re.IGNORECASE,
    )


# Order matters. Every rule is tested against the same text and the first
# match wins, so the specific claims ("usage limit") come before the generic
# ones (a bare 429) that would otherwise swallow them.
_RULES: tuple[tuple[str, re.Pattern[str], re.Pattern[str] | None], ...] = (
    (
        "usage",
        re.compile(
            r"usage limit|quota|insufficient_quota|out of credit|credit balance"
            r"|no credits?|credits? remaining|add credits"
            # What the codex CLI exits with when a subscription is spent, and
            # it says neither "usage limit" nor "credits". Measured
            # 2026-09-02: the sentence before it ("You've hit your usage
            # limit") matched and this one did not, so whether a spent seat
            # was recorded at all depended on which of the two the runtime
            # happened to catch.
            r"|no usage left|usage left on this account"
            r"|billing|plan limit|exceeded your current",
            re.IGNORECASE,
        ),
        None,
    ),
    (
        "auth",
        re.compile(
            r"unauthori[sz]ed|forbidden|invalid[_ ]api[_ ]key|not logged in"
            r"|logged out|auth(?:entication)?[_ ]?(?:error|failed)|re-?authenticate"
            r"|please log ?in",
            re.IGNORECASE,
        ),
        _status("401|403"),
    ),
    (
        "not_found",
        re.compile(
            r"not found|does not exist|unknown model|no such model",
            re.IGNORECASE,
        ),
        _status("404"),
    ),
    (
        "rate_limit",
        re.compile(r"rate[_ ]?limit|too many requests|slow down", re.IGNORECASE),
        _status("429"),
    ),
    (
        # The request reached them and they rejected its SHAPE. Distinct from
        # `server` because no amount of waiting fixes it and distinct from
        # `usage` because the account is fine: the fix is a parameter, and
        # naming which one is the whole value. Brave answered a 422 carrying
        # `{"type":"enum","loc":["query","country"]}` and the runtime recorded
        # `http_error`, so seven searches failed and the rejected parameter
        # was never on a screen.
        "schema",
        re.compile(
            # `malformed` must name what was malformed. Bare, it matches
            # h11's `malformed chunk footer` and `malformed data`, which httpx
            # raises as `RemoteProtocolError` — a corrupted connection
            # mid-stream, which is transient, and which is in
            # `errors.py::_TRANSIENT_EXC_NAMES` precisely because retrying
            # fixes it. Reading it as `schema` would have put it in
            # `_NEVER_RETRY` and advanced the chain instead of retrying.
            r"invalid[_ ]request|unprocessable"
            r"|malformed (?:request|json|body|payload|input|syntax)"
            r"|invalid[_ ](?:parameter|argument|value)|schema"
            r"|unrecogni[sz]ed (?:argument|parameter|field)"
            r"|missing required (?:field|parameter|argument)"
            # A model that does not exist is a name in the config, so the fix
            # is a parameter and not a retry. `unknown model` is what three of
            # the four providers say; `not found` is what the fourth says.
            r"|shape mismatch|invalid json|bad ?request"
            r"|context[_ ]length|context window|maximum context",
            re.IGNORECASE,
        ),
        _status("400|422"),
    ),
    (
        "server",
        re.compile(
            r"internal server error|bad gateway|service unavailable|overloaded"
            r"|ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN"
            r"|socket hang up|network|stream (?:error|disconnected|closed)"
            # The transport words `kernel/adapters/errors.py` used to keep in
            # a private list at the bottom of `classify_exception`. They mean
            # here exactly what they meant there, and holding them in one
            # place is the point of this module.
            r"|connection (?:refused|reset|error|aborted|closed)|connect error"
            r"|timed out|timeout|temporarily unavailable|unreachable",
            re.IGNORECASE,
        ),
        _status(r"5\d\d"),
    ),
)


# What a status code means when the body says nothing a rule recognises. Only
# the codes whose meaning is a REFUSAL are here: 404, 409 and 410 are real
# failures with no shape in this vocabulary, and inventing one for them would
# be the confident wrong bucket this module exists to refuse. They stay
# `unknown`, which is honest, and the retry decision reads them off the status
# table in `kernel/adapters/errors.py` where it always did.
_STATUS_KIND: tuple[tuple[frozenset[int], FaultKind], ...] = (
    (frozenset({401, 403}), "auth"),
    (frozenset({429}), "rate_limit"),
    (frozenset({400, 422}), "schema"),
    (frozenset({500, 502, 503, 504, 529}), "server"),
)


def classify(text: str | None, *, status: int | None = None) -> FaultKind:
    """The kind of refusal `text` names, or `unknown`.

    `text` is everything the call produced that could carry a cause --- both
    streams together, because which one holds it depends on how far the
    provider got, and neither does so reliably.

    `status` is consulted only when the words say nothing a rule recognises,
    and it is the reason a rejected parameter stops reading as `http_error`:
    Brave answered `{"type":"enum","loc":["query","country"]}`, which is a
    perfectly clear rejection and contains not one word this module could
    match. The number it came with is the part that is readable.
    """
    haystack = text or ""
    for kind, words, codes in _RULES:
        if not haystack.strip():
            break
        if words.search(haystack) or (codes is not None and codes.search(haystack)):
            return kind  # type: ignore[return-value]
    if status is not None:
        for codes_set, kind in _STATUS_KIND:
            if status in codes_set:
                return kind
    return "unknown"


@dataclass(frozen=True)
class ProviderFault:
    """One failure, attributed and quoted.

    `origin` answers one question: **did we get as far as asking them?**

    `ours` means we did not. A binary that will not launch, a chain that would
    not build, a timeout value the config never set. Nothing about the
    provider is known, and saying anything about their health would be a
    guess.

    `theirs` means we did. Usually `detail` is then their own sentence, kept
    verbatim; where they failed without saying anything — a call that timed
    out, a reply with no text in it — `detail` is what we observed of them
    instead, and `kind` says which.
    """

    origin: Literal["ours", "theirs"]
    kind: FaultKind
    detail: str

    def __post_init__(self) -> None:
        # The TAIL, not the head. A CLI that fails prints its banner, its
        # session id and its model first and the thing that went wrong last,
        # so truncating from the end keeps the noise and throws away the
        # answer. The ellipsis says a cut happened rather than leaving the
        # operator to wonder why a sentence starts mid-word.
        detail = " ".join(self.detail.split())
        if len(detail) > MAX_FAULT_CHARS:
            # The ellipsis counts. Prepending it after slicing to the cap made
            # every truncated detail 404 characters against a constant that
            # said 400, and that constant is the only thing bounding how much
            # provider text is written to disk.
            detail = _ELLIPSIS + detail[-(MAX_FAULT_CHARS - len(_ELLIPSIS)):]
        object.__setattr__(self, "detail", detail)

    @property
    def line(self) -> str:
        """One line for a person, saying which question was answered."""
        if self.origin == "ours":
            return f"the check could not run: {self.detail}"
        return self.detail or f"the provider failed ({self.kind})"


def ours(what_stopped_us: str) -> ProviderFault:
    """A fault in our own probe. Nothing about the provider is known."""
    return ProviderFault(origin="ours", kind="check_failed", detail=what_stopped_us)


def theirs(provider_text: str, *, status: int | None = None) -> ProviderFault:
    """A fault the provider reported, kept in its own words."""
    return ProviderFault(
        origin="theirs", kind=classify(provider_text, status=status), detail=provider_text
    )


def from_exception(exc: BaseException) -> ProviderFault:
    """A call that reached a provider and raised.

    `theirs`, because the call was dispatched: whatever went wrong is on their
    side of the wire, and `classify` reads their own message for the shape.
    An exception it cannot place stays `unknown` and keeps the text, which is
    worth more than a confident wrong bucket.

    Every probe in `scheduler/tasks/_probes/` goes through this, so a provider
    the operator wires tomorrow is attributed the same way as the two that
    ship. The alternative was six failure branches each inventing its own
    evidence shape, which is what they were.
    """
    return theirs(f"{type(exc).__name__}: {exc}")


def unanswered(seconds: float) -> ProviderFault:
    """A provider that was reached and said nothing in time."""
    return ProviderFault(
        origin="theirs", kind="no_answer", detail=f"no answer within {seconds:g}s"
    )


def evidence(fault: ProviderFault, **extra: object) -> dict[str, object]:
    """The three fields every probe records, plus whatever it knows besides.

    One shape across every tier and kind, so a reader of
    `runtime/logs/provider-health/` never has to learn which probe wrote a row
    before knowing whether the fault was ours.
    """
    return {"origin": fault.origin, "kind": fault.kind, "reason": fault.line, **extra}


__all__ = [
    "CLASSIFIED_KINDS",
    "FAULT_ORIGIN_KEY",
    "FAULT_OURS",
    "MAX_FAULT_CHARS",
    "FaultKind",
    "ProviderFault",
    "classify",
    "evidence",
    "from_exception",
    "ours",
    "theirs",
    "unanswered",
]
