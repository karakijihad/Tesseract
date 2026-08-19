"""What the operator actually agreed to, kept apart from what is on disk.

Its own file, and the separation is the point. The capability artifact is
rewritten from scratch by every pass — it describes the machine, and the
machine changes. An ANSWER does not: it was given once, by a person, and it
has to survive every rewrite, every schema bump, and a pass that fails
halfway. Keeping the two in one file meant a consent write racing a pass
write, and the loser of that race is a question the operator gets asked
again.

Three answers, and the third is the one that matters:

- `granted` — go ahead. Repairing it later needs no new question.
- `declined` — do not, and do not ask again on every launch.
- `never_asked` — nobody has put the question. A dependency introduced by a
  LATER version starts here, which is what makes the install ask about it
  rather than assume the answer given to something else.

`enabled: false` in config cannot express the third, which is why this file
exists at all: a lane the operator switched off and a lane that was never
reached look identical there, and "may I fix this without asking?" has
opposite answers for them.

Lives under `runtime/`, beside `provisioned.json` and `hardware-profile.json`.
That is a decision rather than a default: an answer feels portable, but the
questions are about THIS install — whether these wheels suit this card,
whether this disk has room — and a second machine walks its own first run,
where it is asked properly rather than inheriting a verdict about different
hardware.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from tesseract.capability.state import (
    AUTHORITATIVE_ORIGINS,
    Consent,
    ConsentOrigin,
    DependencyRecord,
    now_iso,
)

logger = logging.getLogger(__name__)

_FILENAME = "consent.json"

#: Written by the shell when it provisions without a setup form — the splash
#: window failed to open, so nobody was asked anything. Its presence changes
#: one thing, and it is the thing that closes `b1049faf`: `enabled: true` in
#: the SHIPPED catalog stops counting as an answer on this machine.
_DEFERRED_FILENAME = "setup-deferred.json"

#: The dependency kinds an unanswered provisioning run skips, and therefore the
#: only ones whose config-derived consent is a fiction. `runtime` is absent on
#: purpose: the venv is installed on every path, including that one, so it was
#: never in question.
#:
#: The browser engine used to be named here too, and was not moved when it
#: became an optional extra — it is a `service` now, and `system.py` records it
#: as one. Leaving it a `runtime` left its shipped `enabled: true` reading as
#: consent on an install where nobody was asked anything.
_DEFERRABLE_KINDS = frozenset({"model", "service", "packages"})


def setup_deferred() -> bool:
    """Whether first-run setup never ran on this install.

    `ConsentOrigin.CONFIG` treats the live config asking for something as
    consent, and that is right when a person walked the form: declining a lane
    there writes `enabled: false` and nothing is fetched for it. It is a
    fiction when the form never opened — the config then holds the SHIPPED
    defaults, which nobody chose, and reading them as consent is how a failed
    splash window turned into several gigabytes on a stranger's machine.

    Cleared by one thing only: the setup form being answered, whether on the
    first run or on a later launch that could finally show it — the shell
    removes the file at the moment those answers land. A Settings toggle does
    not clear it, and that asymmetry is the point: answering one question
    outranks this per dependency, because it is a real answer, while the ones
    still unanswered stay unanswered, which is the truth about this machine.
    Only the form answers all of them at once.

    Total: an unreadable runtime directory answers False, keeping today's
    behaviour rather than silently switching an ordinary install into
    ask-first mode.
    """
    from tesseract.paths import runtime_dir

    try:
        return (runtime_dir() / _DEFERRED_FILENAME).is_file()
    except OSError as exc:  # noqa: BLE001 — a path that cannot be read is not a marker
        logger.info("consent: could not check for %s (%s)", _DEFERRED_FILENAME, exc)
        return False


class ConsentAnswer(BaseModel):
    """One answer, and where it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    consent: Consent
    origin: ConsentOrigin
    #: When it was given. Not decoration: an answer from an install two years
    #: ago about a dependency that has since changed shape is worth showing a
    #: date beside when it is surfaced.
    at: str = ""


class ConsentLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, ConsentAnswer] = Field(default_factory=dict)


def ledger_path() -> Path:
    """Resolved at call time — never an import-time constant."""
    from tesseract.paths import runtime_dir

    return runtime_dir() / _FILENAME


def read_ledger() -> ConsentLedger:
    """Every answer on record. An empty ledger for anything unreadable.

    Total, like the artifact's own reader: this is consulted on the launch
    path, and a malformed file must degrade to "nobody has been asked" rather
    than stop a launch. That degradation is safe in the direction that
    matters — it makes the runtime ask rather than assume.
    """
    path = ledger_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConsentLedger()
    except OSError as exc:
        logger.warning("consent: could not read %s (%s)", path, exc)
        return ConsentLedger()
    try:
        return ConsentLedger.model_validate(json.loads(raw))
    except Exception as exc:  # noqa: BLE001 — any failure is the same outcome
        logger.warning(
            "consent: %s did not validate (%s) — treating as nothing answered", path, exc
        )
        return ConsentLedger()


def _write_ledger(ledger: ConsentLedger) -> Path | None:
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{_FILENAME}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(ledger.model_dump_json(indent=2))
            temp = Path(handle.name)
    except OSError as exc:
        logger.warning("consent: could not stage %s (%s)", path, exc)
        return None
    try:
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("consent: could not install %s (%s)", path, exc)
        temp.unlink(missing_ok=True)
        return None
    return path


def record(
    answers: dict[str, Consent], *, origin: ConsentOrigin
) -> Path | None:
    """Write answers, keeping every one already on record.

    Merged rather than replaced: the first-run form answers the questions it
    asked, and a later Settings toggle answers one. Replacing the file from
    either would silently un-answer everything the other had settled.
    """
    if not answers:
        return None
    ledger = read_ledger()
    stamped = now_iso()
    for dep_id, consent in answers.items():
        ledger.answers[dep_id] = ConsentAnswer(
            consent=consent, origin=origin, at=stamped
        )
    written = _write_ledger(ledger)
    if written is not None:
        logger.info(
            "consent: recorded %s from %s",
            ", ".join(f"{k}={v.value}" for k, v in sorted(answers.items())),
            origin.value,
        )
    return written


def apply(
    record_: DependencyRecord, ledger: ConsentLedger, *, deferred: bool = False
) -> DependencyRecord:
    """Overlay a recorded answer onto a fresh verdict.

    A pass derives consent from the live config (`ConsentOrigin.CONFIG`),
    which is the contract the fetch scripts have always honoured. A real
    answer outranks that — including a `declined` that config would otherwise
    read as wanted, which is the case where getting the precedence backwards
    means downloading something the operator said no to.

    `deferred` is the case where the config is not evidence of anything: the
    setup form never ran, so what it holds are the shipped defaults. A
    config-derived `granted` becomes `never_asked` — which stops the launch
    pass repairing it silently, because nothing was ever agreed to.

    Only for the kinds an unanswered run actually skipped, and that limit is
    load-bearing: `venv` is `runtime`, it is the APP rather than an optional
    download, and it is installed on every path. Downgrading it would make an
    absent interpreter stop being reported — `needs_attention` is `absent AND
    granted` — so the one failure that leaves an install unable to start would
    go silent on exactly the machines this marker exists to protect.
    """
    answer = ledger.answers.get(record_.id)
    if answer is None:
        if (
            deferred
            and record_.consent_origin is ConsentOrigin.CONFIG
            and record_.kind in _DEFERRABLE_KINDS
        ):
            return record_.model_copy(
                update={
                    "consent": Consent.NEVER_ASKED,
                    "consent_origin": ConsentOrigin.UNASKED,
                }
            )
        return record_
    if answer.origin not in AUTHORITATIVE_ORIGINS:
        # Only answers a PERSON gave outrank the live config. A `CONFIG`
        # origin is derived from that same config on every pass, so honouring
        # a stored one would let a lane the operator has since switched off
        # keep a stale `granted` and go on being repaired after they turned it
        # off — the exact inversion the consent rule exists to prevent.
        #
        # Unreachable today: both production callers of `record()` pass an
        # authoritative origin. Checked anyway, because the cost of being
        # wrong here is downloading something nobody agreed to, and "no caller
        # does that yet" is a property of today's callers rather than of this
        # function.
        return record_
    return record_.model_copy(
        update={"consent": answer.consent, "consent_origin": answer.origin}
    )


def apply_all(
    records: dict[str, DependencyRecord], ledger: ConsentLedger | None = None
) -> dict[str, DependencyRecord]:
    """`apply` across a whole pass, reading the ledger and the marker once."""
    ledger = ledger if ledger is not None else read_ledger()
    deferred = setup_deferred()
    return {
        dep_id: apply(rec, ledger, deferred=deferred) for dep_id, rec in records.items()
    }
