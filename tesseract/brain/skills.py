"""Markdown skills loader — the assistant's prose self-extension mechanism ("workshop").

Scans `tesseract/workspace/skills/<name>/SKILL.md` for YAML frontmatter
(`name`, `description`) + a markdown instruction body. `brain/prompt.py::
_build_skills_block` exposes only name/description/path into the prompt
manifest section — the assistant `file_read`s the SKILL.md body on demand.

Skill folders MAY also carry a `scripts/` subdirectory (P6 Task 4b
"workshop" bundling). This loader never reads it: bundled scripts run
through the existing bash/subprocess ASK path, not through this loader —
no new execution surface, no registry writes.

Malformed or unreadable skill folders are SKIPPED, never a boot/prompt-build
failure. Each skip logs at ERROR level so it reaches the Mirror pulse feed
(`mirror/server/log_forwarder.py::MirrorLogHandler` forwards ERROR+ only —
same idiom as the permissions-drift logging in `brain/boot.py`).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Same shape, same reason as `agents/loader.py`: `\s` matches newlines, so
# `\s*\n` makes a run of blank lines ambiguous and an unclosed frontmatter
# quadratic. Skills are self-authored, so the input is generated too.
_FRONTMATTER_RE = re.compile(r"^---[^\S\n]*\n(.*?)\n---[^\S\n]*\n", re.DOTALL)

SKILL_FILENAME = "SKILL.md"

# A hostile/corrupt SKILL.md must not be read into memory whole — cap at
# 256 KiB (PTY_LINE_CAP idiom: a code-level bound, not a config key).
SKILL_MD_MAX_BYTES = 262_144

# Quarantine — mirrors agents/loader.py's pending/rejected split.
# A skill drafted unattended lands in `skills/pending/<name>/SKILL.md` and is
# NEVER surfaced live until the operator promotes it; a rejected draft is
# archived in `skills/rejected/<name>/`. Both dirnames (and __pycache__) are
# skipped by the live scan so a quarantined draft can't rejoin the active set.
SKILL_PENDING_DIRNAME = "pending"
SKILL_REJECTED_DIRNAME = "rejected"
#: Directories under `workspace/skills/` that are not skills. The quarantine
#: trees hold skill FOLDERS, and a draft in one is inert until promoted, so
#: anything walking this directory has to exclude them by name rather than by
#: guessing from whether a SKILL.md sits at the top. Public because the
#: refinement job walks the same directory and must reach the same answer.
SKIP_DIRNAMES = frozenset({SKILL_PENDING_DIRNAME, SKILL_REJECTED_DIRNAME, "__pycache__"})


#: The lifecycle a playbook may declare. A revision is `draft` until it has
#: been used, `active` while it is the one the assistant reaches for, and
#: `retired` when a later revision replaced it or it measured worse than the
#: one before. Closed: `playbook_contract` reports anything else.
PLAYBOOK_STATUSES = ("draft", "active", "retired")

#: Frontmatter keys that make a skill a PLAYBOOK. Declaring any one of them
#: is declaring the whole contract, which `playbook_contract.gaps_for_skill`
#: then checks. A skill declaring none is a plain skill and nothing here
#: applies to it. `allowed-tools` is deliberately absent: it is the interop
#: field a plain skill may carry too.
PLAYBOOK_KEYS = frozenset({
    "use_when", "not_when", "trigger", "preconditions", "steps",
    "forbidden-tools", "expected_result", "failure_modes", "evidence",
    "status", "confidence",
})

#: Everything a playbook owes: the keys above plus the two interop keys a
#: plain skill may also carry, which is why those two do not make one.
CONTRACT_KEYS = PLAYBOOK_KEYS | {"allowed-tools", "version"}


@dataclass(frozen=True)
class Step:
    """One step of a playbook: what to do, and the tool it does it with.

    `tool` is empty for a step the model takes without calling anything. A
    named tool is checked against the registry at boot, because a playbook
    whose step names a tool that is not there cannot run.
    """

    do: str
    tool: str = ""


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    dirname: str
    # Agent Skills standard — optional frontmatter carried through for
    # interop with the peer ecosystems (docs.anthropic.com/en/docs/claude-code/
    # skills). name/description stay required; these are best-effort.
    version: str = ""
    license: str = ""
    allowed_tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    # The playbook half. A procedure that worked, written down on the contract
    # a tool's `use_when`/`not_when` and a manifest entry's summary already
    # use: what shape of problem it answers, what must hold first, the steps
    # and the tool each one uses, what done looks like, what goes wrong, and
    # the turns it was learned from. Parsed tolerantly here; whether the
    # declaration is complete is `playbook_contract`'s question, asked at
    # boot, so a half-written playbook is a reported gap and never a skill
    # that silently fails to load.
    use_when: str = ""
    not_when: str = ""
    trigger: str = ""
    preconditions: tuple[str, ...] = ()
    steps: tuple[Step, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    expected_result: str = ""
    failure_modes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    status: str = ""
    confidence: float | None = None
    #: Which contract keys the frontmatter actually declared, so the contract
    #: can tell a field left empty from one never written, and so a plain
    #: skill is never held to a contract it did not sign.
    declared: frozenset[str] = frozenset()

    @property
    def is_playbook(self) -> bool:
        return bool(self.declared & PLAYBOOK_KEYS)


def _load_skill(folder: Path) -> SkillEntry | None:
    """Parse one skill folder's SKILL.md. Returns None (logged) on any
    malformation; never raises — one bad skill must not break the rest."""
    skill_md = folder / SKILL_FILENAME
    if not skill_md.exists():
        return None  # not a skill folder — nothing to warn about

    try:
        size = skill_md.stat().st_size
    except OSError as exc:
        logger.error("skills: %s unreadable (%s) — skipping", skill_md, exc)
        return None
    if size > SKILL_MD_MAX_BYTES:
        logger.error(
            "skills: %s exceeds %d bytes (%d) — skipping",
            skill_md, SKILL_MD_MAX_BYTES, size,
        )
        return None

    try:
        raw = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8
        # SKILL.md (plausible on Windows) must skip+pulse-warn like any
        # other malformed skill, never raise past load_skills.
        logger.error("skills: %s unreadable (%s) — skipping", skill_md, exc)
        return None

    match = _FRONTMATTER_RE.match(raw)
    if not match:
        logger.error("skills: %s missing YAML frontmatter — skipping", skill_md)
        return None

    try:
        fm: Any = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.error("skills: %s frontmatter YAML invalid (%s) — skipping", skill_md, exc)
        return None

    if not isinstance(fm, dict):
        logger.error("skills: %s frontmatter must be a mapping — skipping", skill_md)
        return None

    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    if not name or not description:
        logger.error(
            "skills: %s frontmatter missing required name/description — skipping",
            skill_md,
        )
        return None

    return SkillEntry(
        name=name,
        description=description,
        dirname=folder.name,
        version=str(fm.get("version") or "").strip(),
        license=str(fm.get("license") or "").strip(),
        allowed_tools=_parse_allowed_tools(fm.get("allowed-tools")),
        metadata=fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {},
        use_when=_text(fm.get("use_when")),
        not_when=_text(fm.get("not_when")),
        trigger=_text(fm.get("trigger")),
        preconditions=_lines(fm.get("preconditions")),
        steps=_parse_steps(fm.get("steps")),
        forbidden_tools=_parse_allowed_tools(fm.get("forbidden-tools")),
        expected_result=_text(fm.get("expected_result")),
        failure_modes=_lines(fm.get("failure_modes")),
        evidence=_lines(fm.get("evidence")),
        status=_text(fm.get("status")),
        confidence=_number_or_none(fm.get("confidence")),
        declared=frozenset(k for k in CONTRACT_KEYS if k in fm),
    )


def _text(raw: Any) -> str:
    return str(raw).strip() if raw is not None else ""


def _lines(raw: Any) -> tuple[str, ...]:
    """A list of strings, or one string, as a tuple. Anything else is empty:
    the contract reports the field as missing rather than this guessing."""
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return ()


def _parse_steps(raw: Any) -> tuple[Step, ...]:
    """`steps:` as declared: a list of `{do, tool}` mappings, or bare strings
    for a step that calls nothing. A mapping with no `do` is dropped, and the
    contract sees a shorter list than the file wrote, which it reports."""
    if not isinstance(raw, (list, tuple)):
        return ()
    steps: list[Step] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            steps.append(Step(do=item.strip()))
        elif isinstance(item, dict):
            do = _text(item.get("do"))
            if do:
                steps.append(Step(do=do, tool=_text(item.get("tool"))))
    return tuple(steps)


def _number_or_none(raw: Any) -> float | None:
    """`confidence` is `None` where nothing measured it, and a bare word in
    the field is the same as nothing rather than a number invented for it."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def _parse_allowed_tools(raw: Any) -> tuple[str, ...]:
    """Normalize the Agent Skills `allowed-tools` frontmatter to a tuple.

    The standard accepts either a YAML list or a space/comma-separated string
    (docs show `Read Grep`); both collapse to an ordered tuple of tool names.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(t for t in re.split(r"[,\s]+", raw.strip()) if t)
    if isinstance(raw, (list, tuple)):
        return tuple(str(t).strip() for t in raw if str(t).strip())
    return ()


def load_skill_folder(folder: Path) -> SkillEntry | None:
    """Parse a single `<folder>/SKILL.md`. Public wrapper over the loader used
    by the promote/create tools to validate a draft parses before moving it.
    Returns None (logged) on any malformation; never raises."""
    return _load_skill(folder)


def load_skills(skills_dir: Path) -> list[SkillEntry]:
    """Return every well-formed skill under `skills_dir`, folder-name sorted.

    Missing `skills_dir` returns []. An unreadable `skills_dir` itself
    (permissions, race) also returns [] — never raises past this function.
    """
    if not skills_dir.exists():
        return []
    try:
        folders = sorted(
            (
                p for p in skills_dir.iterdir()
                if p.is_dir() and p.name not in SKIP_DIRNAMES
            ),
            key=lambda p: p.name,
        )
    except OSError as exc:
        logger.error("skills: %s unreadable (%s) — skipping skills section", skills_dir, exc)
        return []

    entries: list[SkillEntry] = []
    for folder in folders:
        entry = _load_skill(folder)
        if entry is not None:
            entries.append(entry)
    return entries


#: Where a replaced revision goes: `<name>/history/<version>/SKILL.md`.
#: Inside the skill's own folder, so `load_skills` never lists it (it reads
#: one `SKILL.md` per top-level folder) and `skill_usage.skill_name_for_path`
#: never counts a read of it as a load (it wants exactly `<name>/SKILL.md`).
SKILL_HISTORY_DIRNAME = "history"


def keep_predecessor(folder: Path, live: SkillEntry, proposed: SkillEntry) -> str | None:
    """Before a revision replaces the live SKILL.md, keep the live one.

    A revision never overwrites its predecessor, because the whole point of a
    version is that a later one which measures worse can be compared against,
    and returned to, a record that still exists. Returns an error string and
    keeps nothing when the proposal is not a later revision, or when the
    archive slot is already taken (which would be overwriting a predecessor
    after all). A plain skill keeps nothing and is replaced as before: its
    `version` is the interop field, free-form, and orders nothing.
    """
    if not live.is_playbook:
        return None
    from tesseract.brain.playbook_contract import version_number

    before = version_number(live.version)
    after = version_number(proposed.version)
    if before is None or after is None:
        return (
            f"version {live.version!r} to {proposed.version!r} cannot be ordered; "
            "a revision carries a whole number greater than the one before it"
        )
    if after <= before:
        return (
            f"version {proposed.version!r} is not later than the live "
            f"{live.version!r}; a revision never overwrites its predecessor"
        )
    slot = folder / SKILL_HISTORY_DIRNAME / live.version
    if (slot / SKILL_FILENAME).exists():
        # A kept copy that IS the live file is a replace that died between
        # the archive and the swap: the slot is this revision's, not a
        # predecessor's, and refusing it would refuse every retry for good.
        try:
            same = (slot / SKILL_FILENAME).read_bytes() == (folder / SKILL_FILENAME).read_bytes()
        except OSError:
            same = False
        if same:
            return None
        return f"history already holds version {live.version!r}; refusing to overwrite it"
    try:
        slot.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / SKILL_FILENAME, slot / SKILL_FILENAME)
    except OSError as exc:
        return f"could not keep version {live.version!r}: {exc}"
    return None


_STATUS_LINE_RE = re.compile(r"^status:[^\n]*$", re.MULTILINE)
_VERSION_LINE_RE = re.compile(r"^version:[^\n]*$", re.MULTILINE)


def stamp_playbook_version(proposed_markdown: str, number: int) -> str:
    """Write `number` into a proposed SKILL.md's frontmatter `version`.

    The revision number is the runtime's, never the author's. A version is an
    ordering key: `keep_predecessor` archives the predecessor under it and
    refuses anything that does not sort above the live one, so a wrong value
    is not a typo but a refusal, and the model that writes the markdown kept
    proposing `3.1` against a live `3` however plainly the field said whole
    number. Restating the instruction had already been tried; this takes the
    field out of the author's hands instead.

    The line is replaced in place rather than the frontmatter re-serialised,
    so the rest of the file is byte for byte what the author wrote
    (`set_skill_status`'s reason, and the same idiom). Returns the markdown
    unchanged when it has no frontmatter, which is a proposal the loader is
    about to reject anyway.
    """
    match = _FRONTMATTER_RE.match(proposed_markdown)
    if not match:
        return proposed_markdown
    block = match.group(1)
    if _VERSION_LINE_RE.search(block):
        block = _VERSION_LINE_RE.sub(f"version: {number}", block, count=1)
    else:
        block = f"{block}\nversion: {number}"
    return proposed_markdown[: match.start(1)] + block + proposed_markdown[match.end(1):]


def set_skill_status(folder: Path, status: str) -> str | None:
    """Rewrite one line of the live SKILL.md's frontmatter: its `status`.

    A status is lifecycle, not a revision: retiring a playbook that measured
    worse than the one before it does not make a new version and keeps
    nothing under `history/`. The line is replaced in place rather than the
    frontmatter re-serialised, so the rest of the file is byte for byte what
    the author wrote. Returns an error string or None.
    """
    if status not in PLAYBOOK_STATUSES:
        return f"status {status!r} is not one of {', '.join(PLAYBOOK_STATUSES)}"
    path = folder / SKILL_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"could not read {path}: {exc}"
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return f"{path} has no frontmatter to set a status in"
    block = match.group(1)
    if _STATUS_LINE_RE.search(block):
        block = _STATUS_LINE_RE.sub(f"status: {status}", block, count=1)
    else:
        block = f"{block}\nstatus: {status}"
    updated = raw[: match.start(1)] + block + raw[match.end(1):]
    tmp = path.with_suffix(".md.tmp")
    try:
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return f"could not write {path}: {exc}"
    return None


def replace_skill_body(
    skills_dir: Path,
    name: str,
    proposed_markdown: str,
    *,
    tool_names: frozenset[str] | None = None,
) -> str | None:
    """Validate a proposed SKILL.md and atomically replace the live one.

    The one path that changes a live skill, called by the refinement card's
    approve route and by `skill_refine` once its gate is answered. Refuses
    before touching anything when the proposal fails the loader round-trip or
    names a different skill, and, for a playbook, when it would not pass the
    door a new one goes through (`skill_create.refuse_playbook`: a tool the
    runtime lacks, a credential-bearing path, a path outside the home tree).
    A playbook's revision number is stamped by the runtime before any of that
    (`stamp_playbook_version`), so `keep_predecessor` can only refuse over a
    live version that cannot be ordered or a slot already taken, never over
    arithmetic the author got wrong. If the replace itself fails after the
    archive was made, the archive is taken back, so the slot is not consumed
    by a revision that never landed. Returns an error string or None.
    """
    import tempfile

    target = skills_dir / name / SKILL_FILENAME
    if not target.exists():
        return f"no active skill {name!r} to refine at {target}"
    live = load_skill_folder(skills_dir / name)
    if live is None:
        return f"the live skill {name!r} does not parse, so nothing can be kept before replacing it"
    # A retired revision was withdrawn on measurement, and a rewrite is not
    # the way back: returning to a playbook is a person's act on the
    # retirement card. Without this the demotion is one-way only by accident.
    # The card's own `base_sha256` catches the common route here, because
    # retiring rewrites the frontmatter and so moves the hash, but that guard
    # is skipped for a card carrying no hash and it is not this function's to
    # rely on. `skill_refine` reaches here with no hash at all.
    if live.status == "retired":
        return (
            f"{name} v{live.version} is retired, so nothing reads it and a "
            "rewrite of it would not be carried. Make it active again with "
            "`playbook_judge` keep if it should come back, then refine it."
        )

    # The revision number is the runtime's. Stamped before validation so the
    # text written and the entry loaded from it agree, and so `refuse_playbook`
    # reads what will land. A live version that cannot be ordered is NOT
    # stamped over: that is a real broken state and `keep_predecessor` says so.
    if live.is_playbook:
        from tesseract.brain.playbook_contract import version_number

        live_number = version_number(live.version)
        if live_number is not None:
            proposed_markdown = stamp_playbook_version(proposed_markdown, live_number + 1)

    tmp_root = Path(tempfile.mkdtemp())
    tmp_folder = tmp_root / name
    tmp_folder.mkdir(parents=True, exist_ok=True)
    try:
        (tmp_folder / SKILL_FILENAME).write_text(proposed_markdown, encoding="utf-8")
        entry = load_skill_folder(tmp_folder)
        if entry is None:
            return "proposed SKILL.md failed loader validation (frontmatter/size)"
        if entry.name != name:
            return f"proposed frontmatter name {entry.name!r} must match {name!r}"
        if entry.is_playbook:
            from tesseract.kernel.tools.skill_create import refuse_playbook

            refused = refuse_playbook(proposed_markdown, name, tool_names)
            if refused:
                return refused
        kept = keep_predecessor(skills_dir / name, live, entry)
        if kept is not None:
            return kept
    finally:
        try:
            (tmp_folder / SKILL_FILENAME).unlink(missing_ok=True)
            tmp_folder.rmdir()
            tmp_root.rmdir()
        except OSError:
            pass

    tmp = target.with_suffix(".md.tmp")
    try:
        tmp.write_text(proposed_markdown, encoding="utf-8")
        os.replace(str(tmp), str(target))
    except OSError as exc:
        stuck = _take_back_archive(skills_dir / name, live)
        return f"skill refinement write failed: {exc}" + (f"; {stuck}" if stuck else "")
    return None


def _take_back_archive(folder: Path, live: SkillEntry) -> str | None:
    """Remove the history slot `keep_predecessor` just made, if it still holds
    exactly the live file: a replace that failed left the live revision in
    place, and a slot that stayed would refuse every retry of the same
    revision for good. Returns a sentence naming the slot when it could not
    be removed, so the caller's error says what to delete by hand rather than
    leaving a refusal nobody can explain."""
    if not live.is_playbook or not live.version:
        return None
    slot = folder / SKILL_HISTORY_DIRNAME / live.version / SKILL_FILENAME
    try:
        if slot.exists() and slot.read_bytes() == (folder / SKILL_FILENAME).read_bytes():
            slot.unlink()
            slot.parent.rmdir()
    except OSError as exc:
        logger.warning("skills: could not take back the archive at %s", slot, exc_info=True)
        return (
            f"the copy kept at {slot} could not be removed ({exc}); delete it "
            "before retrying, or the retry is refused as overwriting a kept revision"
        )
    return None


def add_evidence(folder: Path, turn_ids: list[str], *, activate: bool = False) -> str | None:
    """Record the turns that supported a playbook, and activate a draft.

    The second-success rule lives here: a draft playbook whose steps carried
    another task through is a procedure that has now worked twice, and it
    becomes `active`. The frontmatter block is re-serialised (the order kept,
    comments not), which is acceptable because a playbook is a machine-read
    declaration and its body, where a person writes, is untouched. Returns an
    error string or None.
    """
    path = folder / SKILL_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"could not read {path}: {exc}"
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return f"{path} has no frontmatter"
    try:
        fm: Any = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        return f"{path} frontmatter invalid: {exc}"
    if not isinstance(fm, dict):
        return f"{path} frontmatter must be a mapping"
    have = [str(t) for t in (fm.get("evidence") or []) if str(t).strip()]
    added = 0
    for turn_id in turn_ids:
        if turn_id and turn_id not in have:
            have.append(turn_id)
            added += 1
    fm["evidence"] = have
    # Only a turn the playbook had not seen counts as a second success. A
    # task read twice (a pass that failed after writing, a position that did
    # not move) brings the same turns back, and they must not activate the
    # draft they were written from.
    if activate and added and str(fm.get("status") or "") == "draft":
        fm["status"] = "active"
    block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    updated = raw[: match.start(1)] + block + raw[match.end(1):]
    tmp = path.with_suffix(".md.tmp")
    try:
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return f"could not write {path}: {exc}"
    return None


def list_history(folder: Path) -> list[SkillEntry]:
    """Every kept revision of one skill, oldest first by version number."""
    history = folder / SKILL_HISTORY_DIRNAME
    if not history.exists():
        return []
    from tesseract.brain.playbook_contract import version_number

    kept: list[SkillEntry] = []
    try:
        slots = [p for p in history.iterdir() if p.is_dir()]
    except OSError:
        return []
    for slot in slots:
        entry = _load_skill(slot)
        if entry is not None:
            kept.append(entry)
    return sorted(kept, key=lambda e: version_number(e.version) or 0)


def list_skills_names(skills_dir: Path) -> list[str]:
    """Names of active (well-formed, non-quarantined) skills."""
    return [s.name for s in load_skills(skills_dir)]


def _list_quarantine_skills(skills_dir: Path, dirname: str) -> list[str]:
    """Return names of skills under `skills_dir/<dirname>/` — each a
    `<name>/SKILL.md` folder. Missing/unreadable → []."""
    quarantine = skills_dir / dirname
    if not quarantine.exists():
        return []
    try:
        return sorted(
            p.name for p in quarantine.iterdir()
            if p.is_dir() and (p / SKILL_FILENAME).exists()
        )
    except OSError:
        return []


def list_pending_skills(skills_dir: Path) -> list[str]:
    """Names of quarantined skills awaiting operator promotion."""
    return _list_quarantine_skills(skills_dir, SKILL_PENDING_DIRNAME)


def list_rejected_skills(skills_dir: Path) -> list[str]:
    """Names of operator-rejected skills archived in `rejected/`."""
    return _list_quarantine_skills(skills_dir, SKILL_REJECTED_DIRNAME)


def read_rejection_reason(skills_dir: Path, name: str) -> str:
    """Operator's reason from the reject sidecar, empty string if absent.
    Written next to `rejected/<name>/` as `rejected/<name>.reason.txt`."""
    reason_path = skills_dir / SKILL_REJECTED_DIRNAME / f"{name}.reason.txt"
    try:
        return reason_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
