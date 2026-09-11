"""The capture funnel's admission policy: what may become a memory.

One filter, one place, however many producers write through it. Every rule is
declared here with the prose a surface shows, and the effective posture of each
resolves through three layers:

    code default  <-  config/memory.yaml::capture_policy  <-  <HOME>/runtime/capture-policy.json

The layer that decided is reported alongside the value, so a toggle that looks
stuck is legible rather than mysterious. Both upper layers are sparse: a rule
nobody has an opinion about keeps the shipped default, which is the same shape
`channels.yaml::muted_categories` uses for notification mutes.

**A matcher never lives in configuration.** A regex in YAML is the prose-that-
looked-like-configuration defect coming back through the front door, which is
what this module exists to end: `WHAT_NOT_TO_SAVE.md` claimed for months to
enable these rules by way of a numbered list that nothing read. Config decides
whether a declared rule is on. Code decides what it matches.

**A rule is not always a matcher here.** `secrets` is enforced by the credential
redactor `MemoryStore.write` already runs over every record, and declaring it
rather than re-implementing it is the point: two answers to "what is a
credential" is how one of them goes stale. Such a rule names `enforced_by` and
carries no predicate, and boot refuses a rule that declares neither or both.

Every `key` below is already the `reason` written to `events/writes.jsonl`, so
the ledger that the gauge reads needs no migration and history stays readable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

from tesseract.paths import config_dir, runtime_dir

logger = logging.getLogger(__name__)

_CONFIG_FILE = "memory.yaml"
_SECTION = "capture_policy"


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

#: The SHIPPED floor for a "trivial body": the title is doing all the work and
#: the body is almost always an echo of it. This is the code layer only. What
#: is in force is `trivial_body_floor()`, which reads `memory.yaml` over it, and
#: every caller asks that rather than this: two of them bound this constant at
#: import and would have frozen the shipped number whatever the operator set.
TRIVIAL_BODY_MIN_CHARS = 80


def trivial_body_floor() -> int:
    """How short a body has to be before `trivial_body` stops it.

    Measured on this machine's store when it was made settable: the rule had
    blocked nothing, ever, and nothing on disk was under 139 characters. So the
    number is not the interesting question yet, and being able to move it
    against the gauge without a release is.
    """
    return effective_settings()["trivial_body"]["min_chars"]

#: What the gauge calls the derivation check's refusals. Not a rule in this
#: registry: it is a fact about a record's provenance rather than about its
#: text, it lives in `memory/derivation.py`, and it writes one distinct reason
#: string per chain, so the gauge gathers them under one name to count them.
DERIVATION_REFUSAL = "derivation_depth"


def _any(patterns: list[re.Pattern[str]]) -> Callable[[str], bool]:
    return lambda content: any(p.search(content) for p in patterns)


@dataclass(frozen=True)
class CaptureRule:
    """One reason a candidate memory does not become one."""

    #: Stable id, and already the `reason` in `events/writes.jsonl`.
    key: str
    #: What it blocks, in the operator's words. This is what a surface shows.
    summary: str
    #: What floods the store without it. The field that makes a rule's
    #: keep-or-drop checkable rather than a matter of taste.
    why: str
    #: The shipped posture. Config and the runtime overlay may move it.
    default: bool = True
    #: The predicate. Code, never config. `None` when another part of the write
    #: path enforces this rule, which `enforced_by` then names.
    matches: Callable[[str], bool] | None = None
    #: Where the rule is enforced when it is not enforced here. A location for
    #: a person to read, not an import path: a string that has to stay
    #: importable is a string that goes stale silently.
    enforced_by: str | None = None
    #: Cannot be switched off from any layer or any surface.
    locked: bool = False
    #: The `reason` this rule's blocks are written under in
    #: `events/writes.jsonl`, when that differs from the key. A rule enforced
    #: here logs its own key; one enforced elsewhere logs whatever that code
    #: already wrote, and the gauge has to look for THAT or the rule reads as
    #: having stopped nothing however often it fires.
    ledger_reason: str | None = None

    #: The rule's own numbers, as `(name, shipped default)` pairs. A rule with
    #: one is written in config as a mapping rather than a bare true/false, so
    #: its posture and its measurement stay in one place per rule instead of a
    #: second section beside the first, which is the shape that drifts.
    settings: tuple[tuple[str, int], ...] = ()
    #: One sentence about those numbers, formatted with the effective values.
    #: Here rather than on a surface, for the reason every other word about a
    #: rule is here: the frontend authors nothing about the machine.
    setting_summary: str = ""

    @property
    def counts_under(self) -> str:
        """The ledger reason to count this rule's blocks under."""
        return self.ledger_reason or self.key


#: Order is the order they are tested in, and it decides which reason a body
#: tripping two rules is recorded under. It matches what shipped before this
#: registry existed, so the ledger stays comparable across the change.
CAPTURE_RULES: tuple[CaptureRule, ...] = (
    CaptureRule(
        key="secrets",
        summary=(
            "Credentials are stripped out of a memory before it is written, and "
            "the write is refused if that check cannot run."
        ),
        why=(
            "A memory is the most durable of the places a credential can land: "
            "it outlives the conversation and is read back into later prompts, "
            "and nothing asks before one is written."
        ),
        enforced_by="memory/store.py::MemoryStore.write, via credentials/redaction.py::redact",
        locked=True,
        # A successful strip is silent: the record is written with the value
        # taken out. The only thing this rule writes to the ledger is the
        # refusal when the check cannot run at all, so that is what its count
        # is. Counting `secrets` would have shown zero forever.
        ledger_reason="credential_check_unavailable",
    ),
    CaptureRule(
        key="code_pattern",
        summary="Function bodies, class definitions, imports and try/except blocks.",
        why=(
            "The source file is the truth and it changes without the memory "
            "changing. What is worth keeping is the observation about the code, "
            "not a copy of it that quietly goes wrong."
        ),
        matches=_any(_CODE_PATTERNS),
    ),
    CaptureRule(
        key="git_history",
        summary="Commit hashes and the output of git log, blame, diff and show.",
        why="The repository is authoritative and answers faster than retrieval does.",
        matches=_any(_GIT_PATTERNS),
    ),
    CaptureRule(
        key="ephemeral_task_state",
        summary='"Currently working on", "in-progress", "right now I am", scratch state.',
        why=(
            "It is false within the hour and nothing ever comes back to correct "
            "it. What is being worked on lives in the agenda."
        ),
        matches=_any(_EPHEMERAL_PATTERNS),
    ),
    CaptureRule(
        key="instruction_echo",
        summary="The assistant's own standing instructions, quoted back at itself.",
        why=(
            "They are already loaded on every turn. Recalling them spends the "
            "retrieval budget on text that was never going to be absent."
        ),
        matches=_any(_INSTRUCTION_ECHO_PATTERNS),
    ),
    CaptureRule(
        key="routine_ack",
        summary='Bare acknowledgements: "hello", "thanks", "ok", "got it".',
        why="Turn noise. Nothing recalls it and it crowds what does.",
        matches=_any(_ROUTINE_PATTERNS),
    ),
    CaptureRule(
        key="request_echo",
        summary='Records that narrate what was just asked: "You asked me to", "User requested".',
        why=(
            "The request is already in the conversation. Restating it saves the "
            "question instead of the answer, and the answer was the durable half."
        ),
        matches=_any(_REQUEST_ECHO_PATTERNS),
    ),
    CaptureRule(
        key="turn_summary",
        summary='Meta-commentary about the session: "In this turn", "Last action", "Turn recap".',
        why=(
            "It describes the conversation rather than anything learned in it. "
            "If a durable fact came out of the turn, the fact is what to keep."
        ),
        matches=_any(_TURN_SUMMARY_PATTERNS),
    ),
    CaptureRule(
        key="trivial_body",
        summary="Bodies too short to carry any context of their own.",
        why=(
            "A memory worth keeping carries its context. Under this length it is "
            "almost always the title said twice, which retrieves badly and reads "
            "as noise when it does."
        ),
        matches=lambda content: len(content.strip()) < trivial_body_floor(),
        settings=(("min_chars", TRIVIAL_BODY_MIN_CHARS),),
        setting_summary="A body under {min_chars} characters is too short.",
    ),
)

RULES_BY_KEY: dict[str, CaptureRule] = {rule.key: rule for rule in CAPTURE_RULES}


def check_rules() -> None:
    """Fail boot on a rule that cannot be shown to the operator or enforced.

    Same guard as `Tool.default_posture` and the run manifest, one subsystem
    over, and for the same reason: this registry is rendered on a surface the
    operator makes decisions from, so a rule with no `why` is a switch with no
    stated consequence, and a rule with no implementation is a promise nothing
    keeps. That is the exact defect this module replaced.
    """
    seen: set[str] = set()
    for rule in CAPTURE_RULES:
        if not rule.key:
            raise ValueError("capture rule declares no key")
        if rule.key in seen:
            raise ValueError(f"capture rule {rule.key!r} is declared twice")
        seen.add(rule.key)
        if not rule.summary.strip():
            raise ValueError(f"capture rule {rule.key!r} declares no summary")
        if not rule.why.strip():
            raise ValueError(f"capture rule {rule.key!r} declares no why")
        if (rule.matches is None) == (rule.enforced_by is None):
            raise ValueError(
                f"capture rule {rule.key!r} must declare exactly one of "
                "`matches` (it is enforced here) or `enforced_by` (it is "
                "enforced elsewhere on the write path)"
            )
    # And the config that postures them. This is the one place a malformed
    # section is allowed to raise: at write time it degrades to the shipped
    # defaults so a memory is never lost to a typo, which would leave a
    # misspelled rule key silently doing nothing if boot did not read it here.
    #
    # A file that is not there is a different case and does not raise. The
    # update path replaces config from the templates before the app starts, so
    # the only way to reach this is to delete or empty the file by hand, and
    # refusing to boot over a memory-policy setting is a worse answer than
    # running on the shipped defaults and saying so. `memory.yaml::derivation`
    # is read by the same file and degrades the same way.
    try:
        read_config_policy()
    except FileNotFoundError:
        logger.warning(
            "%s is missing, so the capture rules run on their shipped "
            "defaults. Restore it, or let the next update replace it.",
            _CONFIG_FILE,
        )


def capture_policy_path() -> Path:
    """The operator's per-machine overrides. Never committed."""
    return runtime_dir() / "capture-policy.json"


def read_runtime_policy() -> dict[str, bool]:
    path = capture_policy_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        # Memory writes are unconditional and this is a rule over them: an
        # overlay the operator has broken must not take memory writing down
        # with it. The shipped defaults are a safe place to land.
        # "No overrides", not "shipped defaults": this layer sits ON TOP of
        # `memory.yaml`, so dropping it leaves the config's answer in force,
        # and the old wording named a state the runtime does not go to.
        logger.warning("capture-policy.json unreadable, so none of it applies (%s)", exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("capture-policy.json is not an object, so none of it applies")
        return {}
    kept = {
        key: value
        for key, value in raw.items()
        if key in RULES_BY_KEY and isinstance(value, bool)
    }
    # Said out loud rather than dropped in silence. This file is hand-editable
    # and the config layer raises on the same mistake; going quiet here would
    # be the defect this module replaced, one layer down. It still cannot
    # raise: the panel writes this file and memory writing must survive it.
    dropped = sorted(set(raw) - set(kept))
    if dropped:
        logger.warning(
            "capture-policy.json: ignoring %s. A rule must be named exactly as "
            "it is declared and set to true or false. Declared rules are %s",
            ", ".join(dropped), ", ".join(RULES_BY_KEY),
        )
    return kept


def write_runtime_policy(policy: dict[str, bool]) -> None:
    """Replace the overlay atomically, the way every other runtime file is written.

    A unique temp name and an unlink on failure, matching
    `orchestrator/autonomy/outbound.py::_atomic_write_json`: one fixed sibling
    is a collision between two writers, and a throw part-way leaves it behind
    for good.
    """
    path = capture_policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        tmp.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _config_section() -> dict:
    """`config/memory.yaml::capture_policy`, checked. Raises as the file says.

    Same shape as `derivation.load_derivation_config` and
    `brain.auto_recall.load_auto_recall_config`: the path resolves at call time
    so a config reload is seen, and there is no `.get(..., default)`. The
    section is required. A key naming no declared rule raises rather than
    reading as a setting that does nothing, which is the defect this whole
    module replaced.

    A rule is written as `true`/`false`, or as a mapping when it has numbers of
    its own: `enabled` plus whatever that rule declares in `settings`. One
    entry per rule either way, rather than a second section beside this one
    holding the numbers, which is the arrangement that drifts.
    """
    raw = yaml.safe_load((config_dir() / _CONFIG_FILE).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or _SECTION not in raw:
        raise RuntimeError(f"missing required key '{_SECTION}' in {_CONFIG_FILE}")
    block = raw[_SECTION] or {}
    if not isinstance(block, dict):
        raise RuntimeError(
            f"{_CONFIG_FILE}::{_SECTION} must be a mapping of rule to its setting"
        )
    unknown = sorted(set(block) - set(RULES_BY_KEY))
    if unknown:
        raise RuntimeError(
            f"{_CONFIG_FILE}::{_SECTION} names no such capture rule: "
            f"{', '.join(unknown)}. Declared rules are {', '.join(RULES_BY_KEY)}"
        )
    for key, value in block.items():
        rule = RULES_BY_KEY[key]
        if isinstance(value, bool):
            continue
        if not isinstance(value, dict):
            # Not `bool(value)`. YAML's `false` is a bool and the string
            # `"false"` is not, and coercing turns the second into True: a rule
            # the operator switched off and that is quietly still on.
            raise RuntimeError(
                f"{_CONFIG_FILE}::{_SECTION}.{key} must be true, false, or a "
                "mapping with `enabled` and this rule's own settings"
            )
        if not isinstance(value.get("enabled"), bool):
            raise RuntimeError(
                f"{_CONFIG_FILE}::{_SECTION}.{key} is a mapping, so it must "
                "give `enabled` as true or false"
            )
        declared = {name for name, _ in rule.settings}
        extra = sorted(set(value) - {"enabled"} - declared)
        if extra:
            raise RuntimeError(
                f"{_CONFIG_FILE}::{_SECTION}.{key} has no setting called "
                f"{', '.join(extra)}. It has "
                f"{', '.join(sorted(declared)) if declared else 'none'}"
            )
        for name in declared & set(value):
            number = value[name]
            # `isinstance(True, int)` is True in Python, and a rule whose floor
            # reads `true` would silently become 1.
            if not isinstance(number, int) or isinstance(number, bool):
                raise RuntimeError(
                    f"{_CONFIG_FILE}::{_SECTION}.{key}.{name} must be a whole "
                    "number"
                )
    return block


def read_config_policy() -> dict[str, bool]:
    """Which rules the config layer switches on, however each is written."""
    return {
        key: (value if isinstance(value, bool) else bool(value["enabled"]))
        for key, value in _config_section().items()
    }


def read_config_settings() -> dict[str, dict[str, int]]:
    """The numbers the config layer sets, per rule. Absent means shipped."""
    out: dict[str, dict[str, int]] = {}
    for key, value in _config_section().items():
        if not isinstance(value, dict):
            continue
        declared = {name for name, _ in RULES_BY_KEY[key].settings}
        given = {name: value[name] for name in declared & set(value)}
        if given:
            out[key] = given
    return out


@dataclass(frozen=True)
class RuleState:
    """A rule's effective posture and which layer decided it."""

    rule: CaptureRule
    enabled: bool
    #: "code", "config" or "runtime".
    layer: str
    #: The rule's own numbers in force, shipped defaults where config sets none.
    settings: dict[str, int]
    #: One sentence about those numbers, or None for a rule that has none.
    detail: str | None


#: The last resolution, with what the two files looked like when it was made.
#: Module state rather than per-store, because the answer is the machine's and
#: not a store's. A racing writer costs one extra resolution and no wrong
#: answer: the stamp is read before the parse, so a file that changes in
#: between is seen as changed on the next call.
_RESOLVED: tuple[
    tuple[tuple[str, int, int], tuple[str, int, int]], dict[str, RuleState]
] | None = None


def _sources_stamp() -> tuple[tuple[str, int, int], tuple[str, int, int]]:
    """What the two upper layers look like on disk right now.

    Size beside mtime because a same-tick edit of the same length is the one
    thing mtime alone misses, and both are one `stat` that the parse this
    guards costs many times over.

    The PATH is in the key as well as the stat. Both resolve through
    `TESSERACT_HOME`, so two different homes are two different policies, and a
    key made of stats alone matches whenever neither file exists: `(-1, -1)`
    twice over is the same key for every machine and every test home.
    """
    def stamp(path: Path) -> tuple[str, int, int]:
        try:
            info = path.stat()
        except OSError:
            return (str(path), -1, -1)
        return (str(path), info.st_mtime_ns, info.st_size)

    return (stamp(config_dir() / _CONFIG_FILE), stamp(capture_policy_path()))


def effective_policy() -> dict[str, RuleState]:
    """Every rule's posture and the layer that decided it.

    Resolved from disk the first time and whenever either file has moved,
    because this is asked on the path of every admitted memory and a YAML
    parse per write is a cost the check it replaced never had. Keyed on what
    the files look like rather than on a clock, so a rule switched in the panel
    still changes the very next write with no restart, which is the guarantee
    this phase owes.

    A config the operator has broken must not take memory writing down with it:
    memory writes are unconditional and this is a rule over them, so a
    malformed section degrades to the shipped defaults here and the operator
    hears about it at boot, where `check_rules` lets the same read raise.
    """
    global _RESOLVED
    stamp = _sources_stamp()
    cached = _RESOLVED
    if cached is not None and cached[0] == stamp:
        return cached[1]
    resolved = _resolve_policy()
    _RESOLVED = (stamp, resolved)
    return resolved


def effective_settings() -> dict[str, dict[str, int]]:
    """Every rule's numbers in force. Same cache and same layering as postures."""
    return {key: state.settings for key, state in effective_policy().items()}


def reset_cache() -> None:
    """Forget the resolved policy.

    The key carries both paths, so a test with a home of its own is already
    safe. This exists so that stays true by construction rather than by
    nobody having written the test that shares one.
    """
    global _RESOLVED
    _RESOLVED = None


def _resolve_policy() -> dict[str, RuleState]:
    try:
        config = read_config_policy()
        config_settings = read_config_settings()
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture policy config not applied, using shipped defaults (%s)", exc)
        config, config_settings = {}, {}
    runtime = read_runtime_policy()
    states: dict[str, RuleState] = {}
    for rule in CAPTURE_RULES:
        # Numbers come from the config layer or from the rule's own shipped
        # default. The runtime overlay is postures only: it is written by a
        # switch in the panel, and a panel that could also set a threshold
        # would need a second control this phase did not build.
        settings = dict(rule.settings)
        settings.update(config_settings.get(rule.key, {}))
        detail = rule.setting_summary.format(**settings) if rule.setting_summary else None
        enabled, layer = rule.default, "code"
        if rule.key in config:
            enabled, layer = config[rule.key], "config"
        if rule.key in runtime:
            enabled, layer = runtime[rule.key], "runtime"
        if rule.locked and not enabled:
            # A floor, not a default. Nothing below the registry may lower it,
            # and saying so here means the endpoint's 400 is a courtesy rather
            # than the only thing holding the line.
            logger.warning(
                "capture rule %r is locked and cannot be switched off; "
                "the %s layer tried and was ignored", rule.key, layer,
            )
            enabled, layer = True, "code"
        states[rule.key] = RuleState(
            rule=rule,
            enabled=enabled,
            layer=layer,
            settings=settings,
            detail=detail,
        )
    return states


class CapturePolicy:
    """The admission gate. One instance per store."""

    def __init__(self) -> None:
        # Set by `admits()` on the last call, so `MemoryStore.write` can name
        # the specific rule in the forensic ledger.
        self.last_reason: str | None = None

    def admits(self, content: str) -> bool:
        """Whether this content may enter the store.

        Repairs never reach here. `MemoryStore.write` asks this for an
        ADMISSION, which is a record that is not on disk yet or one whose
        prose is being replaced with something that did not come from the
        store; `MemoryStore._is_an_admission` holds the full property list and
        is the only place that decides it. Note the derivation check answers a
        narrower question, new records only, and deliberately does not share
        this one.
        """
        self.last_reason = None
        states = effective_policy()
        for rule in CAPTURE_RULES:
            if rule.matches is None:
                continue
            state = states[rule.key]
            if not state.enabled:
                continue
            if rule.matches(content):
                self.last_reason = rule.key
                logger.debug("Blocked by capture rule %s", rule.key)
                return False
        return True


def explain_block(reason: str | None) -> str:
    """Why a write did not happen, in the rule's own words.

    One sentence for every door that writes a memory, so `memory_save` and
    `memory_update` cannot describe the same refusal two ways. The rule says
    what it blocks and why it exists; repeating either at a call site would be
    a second copy that goes stale the first time one of them is reworded.
    """
    rule = RULES_BY_KEY.get(reason or "")
    if rule is None:
        return (
            "Not saved. The store refused the write and named no rule, which "
            "usually means the credential check could not run."
        )
    return f"Not saved. {rule.summary} {rule.why}"


def block_counts(days: int = 30, *, store_dir: Path | None = None) -> dict[str, int]:
    """Count what the funnel refused, per reason, over a rolling window.

    Reads `events/writes.jsonl`, which every write path already appends to, so
    the gauge needs no instrumentation of its own. It counts every status that
    means a record did not land, not only rule blocks: the derivation refusal
    and the credential check write to the same ledger, and a gauge that showed
    rules alone would report a quiet funnel while records were being turned
    away beside it.
    """
    if store_dir is None:
        from tesseract.paths import home_dir

        store_dir = home_dir() / "memory-store"
    path = store_dir / "events" / "writes.jsonl"
    counts: dict[str, int] = {}
    if not path.is_file():
        return counts
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001 — a torn line is not a reason to stop counting
                continue
            status = row.get("status")
            if status not in ("blocked", "refused"):
                continue
            # A row this window cannot date is skipped, whatever the reason:
            # a timestamp that is missing, null or numeric rather than a
            # string; one that will not parse; or one that parses without an
            # offset and so cannot be compared with an aware cutoff. Counting
            # any of them makes a long-dead rule look busy, which is the one
            # thing a rolling window is asked to prevent, and skipping is how
            # every other bad row in this loop is already treated.
            stamp = row.get("timestamp")
            if not isinstance(stamp, str):
                continue
            try:
                if datetime.fromisoformat(stamp) < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            # A derivation refusal writes its whole chain into `reason`, so it
            # is one distinct string per chain rather than a category, and it
            # has to be gathered under a name. The STATUS is what says so:
            # `refused` is the derivation check and `blocked` is everything
            # else. Gathering by "a reason I do not recognise" is what this
            # replaced, and it counted `type_mismatch` (memory_save.py, a real
            # reason a record is turned away) as a derivation refusal. An
            # unrecognised reason now keeps its own name and is reported as
            # itself, which is the only honest thing a gauge can do with one.
            if status == "refused":
                counts[DERIVATION_REFUSAL] = counts.get(DERIVATION_REFUSAL, 0) + 1
                continue
            reason = row.get("reason")
            if not isinstance(reason, str) or not reason:
                continue
            counts[reason] = counts.get(reason, 0) + 1
    return counts


__all__ = [
    "CAPTURE_RULES",
    "RULES_BY_KEY",
    "TRIVIAL_BODY_MIN_CHARS",
    "CapturePolicy",
    "CaptureRule",
    "RuleState",
    "block_counts",
    "capture_policy_path",
    "check_rules",
    "effective_policy",
    "effective_settings",
    "read_config_policy",
    "read_config_settings",
    "read_runtime_policy",
    "trivial_body_floor",
    "reset_cache",
    "write_runtime_policy",
]
