"""The capability artifact: one record per dependency, one file per machine.

Three ideas carry this module, and each replaced something that did not work.

**State is three-valued, not two.** "Installed" and "not installed" cannot
express the case this phase exists for — an artifact that is present and is
the wrong one. `stale` is that case, and keeping it apart from `absent` is
what lets the reconciler repair one silently and ask about the other.

**Correctness is a recorded verdict, not a recomputation.** A model file's
digest was verified once, when it was written, against a pin the catalog
named. `VerifiedPin` records that verdict so drift becomes a string
comparison. `pinned_fetch.ensure_files` already refuses to re-hash a present
file on the grounds that reading 1.6 GB per launch costs more than it
protects; a reconciler that re-hashed everything would be that same cost,
multiplied.

**Consent is recorded, not inferred.** A lane the operator declined and a lane
that was never reached both leave `enabled: false` in config, and telling them
apart is the whole basis for acting without asking. `never_asked` is the state
that makes a dependency introduced in a LATER version ask rather than assume.

Reads are total: a missing, truncated or schema-changed file means "never
reconciled", never an exception. This is read on the launch path by surfaces
whose job is to report a problem, and a reporter that dies on a malformed
report is worse than no reporter.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Bumped when a field changes meaning rather than merely appearing. A reader
# holding a newer schema than the file discards it and reconciles again, which
# costs one pass; misreading an old field as a new one costs a wrong answer
# nobody can see is wrong.
SCHEMA_VERSION = 1

_FILENAME = "capability-state.json"


class DependencyState(str, Enum):
    """What is true about one dependency on this machine, right now."""

    #: Present, and matching what this app version names.
    OK = "ok"
    #: Not here. Either declined, or never fetched — `Consent` says which.
    ABSENT = "absent"
    #: Here, and NOT what this app version names.
    STALE = "stale"
    #: The probe could not run. Deliberately distinct from `absent`: "could
    #: not look" and "looked, found nothing" are different answers, and
    #: collapsing them is the defect P2 fixed in the Ollama tag fetch.
    UNKNOWN = "unknown"


class Consent(str, Enum):
    """Whether the operator has agreed to this dependency existing here.

    The axis the reconciler acts on. Repairing something already granted needs
    no question; anything else does, whatever it costs.
    """

    GRANTED = "granted"
    DECLINED = "declined"
    NEVER_ASKED = "never_asked"


class ConsentOrigin(str, Enum):
    """Where a consent answer came from.

    Recorded because the answers are not equally durable: a first-run choice
    was made once about an unfamiliar app, a Settings toggle is a considered
    change, and an assumption is neither.
    """

    FIRST_RUN = "first_run"
    SETTINGS = "settings"
    #: Inferred from the live config asking for it. The weakest of the three,
    #: and the only one re-derived on every pass rather than remembered.
    #:
    #: It is not a fiction: config-says-yes has BEEN the consent signal for as
    #: long as the fetch scripts have existed — declining a lane at setup
    #: writes `enabled: false` and they download nothing. Naming it lets the
    #: reconciler act on today's contract without pretending someone was asked
    #: a question they were not, and lets a real answer outrank it later.
    CONFIG = "config"
    #: Nobody asked. The default for a dependency this install predates.
    UNASKED = "unasked"


#: Origins that represent an answer a PERSON gave. These survive a fresh pass;
#: `CONFIG` deliberately does not, because it is re-derived from config each
#: time — carrying it would let a lane the operator has since switched off keep
#: a stale `granted` and go on being repaired.
AUTHORITATIVE_ORIGINS = frozenset({ConsentOrigin.FIRST_RUN, ConsentOrigin.SETTINGS})


class VerifiedPin(BaseModel):
    """What one file on disk was checked against, when it was written.

    Not a re-derivable fact: the catalog's pin moves when the app updates, and
    the whole point is to compare what a file WAS verified against with what
    is named NOW. Recomputing it from the current catalog would compare the
    catalog to itself and never report drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The upstream location, carrying its own revision — a HuggingFace
    #: commit, a release tag. Half the identity of a pin.
    base_url: str
    #: The other half. A base_url can be re-pointed without the bytes
    #: changing, and a file can change under an unchanged base_url when that
    #: URL names a branch rather than a commit.
    sha256: str


class DependencyRecord(BaseModel):
    """One dependency's verdict."""

    model_config = ConfigDict(extra="forbid")

    #: Stable identifier. Not enumerated here: the producers are
    #: `capability/models.py` (the pinned-file lanes) and `capability/
    #: system.py` (everything else), and a list in this docstring drifted from
    #: both — it named `gpu-wheels`, which nothing produces, and omitted
    #: `ollama-models`, which two callers do.
    id: str
    #: What kind of thing it is, which decides which probe answers for it —
    #: and, through `consent._DEFERRABLE_KINDS`, whether a config-derived
    #: answer survives on an install whose setup form never opened. That
    #: second use is why the value is a decision rather than a label: the
    #: browser engine sat at `runtime` after it became optional, and its
    #: shipped `enabled: true` went on reading as consent.
    kind: str
    state: DependencyState
    consent: Consent = Consent.NEVER_ASKED
    consent_origin: ConsentOrigin = ConsentOrigin.UNASKED
    #: One line, written for a person. Empty when the state speaks for itself.
    reason: str = ""
    #: Download size where it is a fact about the artifact. `None` where it is
    #: resolved per machine (the dependency set, the CUDA wheels) — an
    #: invented figure here would reach the operator as a claim.
    size_mb: int | None = None
    #: What is installed, for the two dependencies that HAVE a version worth
    #: recording: the Ollama binary, whose installer URL is unversioned by
    #: design, and whatever digest a moving tag like `nomic-embed-text:latest`
    #: currently resolves to.
    #:
    #: Empty for everything else, and that is not an omission. Every model in
    #: the catalog is pinned to an upstream commit plus a per-file sha256, so
    #: its "version" is `pins` — recording a second, softer notion of version
    #: beside a digest would give one fact two owners.
    version: str = ""
    #: filename -> the pin it was verified against. Empty for kinds that are
    #: not files.
    pins: dict[str, VerifiedPin] = Field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        """Whether a person should be told about this.

        `absent` with consent DECLINED is not a problem — it is the operator's
        answer being honoured, and reporting it would train them to dismiss
        the report. `unknown` is not a problem either: a probe that could not
        run has found nothing to say, and saying so every launch is noise.
        """
        if self.state is DependencyState.STALE:
            return True
        return self.state is DependencyState.ABSENT and self.consent is Consent.GRANTED

    @property
    def may_repair_silently(self) -> bool:
        """Whether the reconciler may fix this without asking.

        The one rule: repairing something already consented to needs no
        question, because the question was already answered. Everything else
        asks, whatever it costs — a size threshold would let a large download
        the operator never agreed to arrive silently on the grounds that some
        number in a config file was bigger than it.
        """
        return self.consent is Consent.GRANTED and self.state in (
            DependencyState.STALE,
            DependencyState.ABSENT,
        )


class HardwareFacts(BaseModel):
    """What the machine is, folded in from `check_dependencies.collect()`.

    Here rather than in a second file because a dependency verdict is only
    interpretable beside the hardware that decided it: "GPU wheels absent" is
    correct on a machine with no card and a defect on one with a card, and two
    separate artifacts is how those get read apart.
    """

    model_config = ConfigDict(extra="forbid")

    gpu_vendor: str = "unknown"
    gpu_name: str | None = None
    gpu_memory_mb: int | None = None
    gpu_cuda: bool = False
    ram_total_gb: int | None = None
    disk_free_gb: int | None = None
    mic_devices: int | None = None
    python_version: str = ""
    #: Carried because Settings renders them. They belong here rather than in
    #: a second file for the same reason everything else does — the machine's
    #: facts are one artifact, and the previous split is what left a snapshot
    #: sitting in the janitor's prune window.
    node_version: str | None = None
    pnpm_version: str | None = None
    platform: dict[str, str] = Field(default_factory=dict)
    #: The profile `provision_hardware` resolved this machine to, so a change
    #: is detectable without reading a second file.
    profile: str | None = None
    #: The profile's speech-synthesis advice (`kokoro-gpu` / `kokoro-cpu`).
    #: Written to `hardware-profile.json` since P1.5 and read by nobody until
    #: now, which is why a machine that lost its graphics card went on
    #: recommending the voice it could no longer keep up with.
    tts_note: str | None = None


class Advice(BaseModel):
    """Something worth telling a person once.

    Not a dependency verdict: a profile change is a fact about the machine,
    and the runtime has already acted on it. This is the part that says so.

    Deliberately never auto-applied. The voice was chosen in words on the
    first-run form, and hardware does not get to overrule words — the most a
    changed machine may do is point out that a different choice would now keep
    up better.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    at: str = ""


class CapabilityState(BaseModel):
    """Everything one reconcile pass concluded."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    #: UTC ISO-8601. A reader showing "checked 3 days ago" is showing that the
    #: reconciler has not run, which is itself worth seeing.
    checked_at: str
    #: Which boot produced this, so the pass and the log covering it join up —
    #: the same reason `boot_id` became one-per-process in P9.
    boot_id: str = ""
    dependencies: dict[str, DependencyRecord] = Field(default_factory=dict)
    hardware: HardwareFacts = Field(default_factory=HardwareFacts)
    #: Emitted only when something CHANGED, so it is empty on almost every
    #: launch. A list that is usually populated is one people stop reading.
    advice: list[Advice] = Field(default_factory=list)

    @property
    def attention(self) -> list[DependencyRecord]:
        """Dependencies a person should be told about, worst first.

        Sorted `stale` before `absent`: a wrong artifact is running right now
        and producing wrong behaviour, while a missing one is a capability
        that is simply off.
        """
        wanted = [d for d in self.dependencies.values() if d.needs_attention]
        return sorted(
            wanted,
            key=lambda d: (d.state is not DependencyState.STALE, d.id),
        )


def state_path() -> Path:
    """Where the artifact lives — `runtime/capability-state.json`.

    Under `runtime/` because it describes THIS machine, beside
    `hardware-profile.json` and `provisioned.json`. Deliberately NOT under
    `runtime/logs/`, where its predecessor `capability-snapshot.json` sat: that
    tree is the janitor's, pruned by age, and state that expires is state that
    silently stops answering.

    Resolved at CALL time, never as a module constant. An import-time constant
    ignores a `TESSERACT_HOME` set afterwards — which is how a test writes into
    the operator's real tree, and the defect `boot.py`'s reranker path still
    carries.
    """
    from tesseract.paths import runtime_dir

    return runtime_dir() / _FILENAME


def read_state() -> CapabilityState | None:
    """The last pass's conclusions, or None if there has not been one.

    Total by construction. Absent, unreadable, malformed and schema-shifted
    all mean the same thing to every caller — "no answer yet, reconcile" — and
    are worth distinguishing only in the log.
    """
    path = state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("capability: could not read %s (%s)", path, exc)
        return None

    try:
        data = json.loads(raw)
    except ValueError as exc:
        logger.warning("capability: %s is not valid JSON (%s) — treating as unreconciled", path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("capability: %s is not an object — treating as unreconciled", path)
        return None

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        logger.info(
            "capability: %s carries schema %r, this build reads %d — "
            "discarding and reconciling again",
            path, version, SCHEMA_VERSION,
        )
        return None

    try:
        return CapabilityState.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — any validation failure is the same outcome
        logger.warning("capability: %s did not validate (%s) — treating as unreconciled", path, exc)
        return None


def write_state(state: CapabilityState) -> Path | None:
    """Persist a pass's conclusions. Returns the path, or None on failure.

    Written through a temporary file in the same directory and renamed into
    place, so a reader never sees a half-written artifact. `os.replace` is
    atomic on both platforms this ships to, and the same-directory constraint
    is what keeps it on one filesystem — the rename degrades to a copy
    otherwise, which is precisely the non-atomic write being avoided.

    Never raises. This is called from a background pass whose failure must not
    reach the launch it is running beside; a machine that cannot write the
    artifact still runs, it just cannot report.
    """
    path = state_path()
    body = state.model_dump_json(indent=2)
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
            handle.write(body)
            temp = Path(handle.name)
    except OSError as exc:
        logger.warning("capability: could not stage %s (%s)", path, exc)
        return None

    try:
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("capability: could not install %s (%s)", path, exc)
        temp.unlink(missing_ok=True)
        return None
    return path


def now_iso() -> str:
    """UTC timestamp for `checked_at`, in one place so readers can parse one
    shape."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
