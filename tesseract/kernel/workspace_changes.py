"""Generic propose/commit primitives for agent-initiated workspace changes.

Mental model: the assistant is a colleague sending change requests. Any mutation
of an operator-owned workspace `.md` file (SOUL.md, USER.md, OPERATING.md,
etc.) routes through this module:

    propose_change tool  ─►  WorkspaceEvent(kind="change_proposal")  ─►  inbox row
    operator clicks Approve in workspace  ─►  apply_change()  ─►  file mutated

No tool may write these files directly. The workspace `post_decision`
endpoint is the single commit point. Concurrency is guarded by an
`expected_hash_before` snapshot taken at propose time — if the file
mutates between propose and approve the commit fails with
`ConcurrentModificationError` and the operator re-reviews against the
fresh diff.

This module also carries YAML-aware actions for catalog edits proposed
by the knowledge-keeper. Three actions
(``insert_under_path`` / ``update_field`` / ``append_to_list_at_path``)
land via :func:`apply_yaml_change`, with a drift check, a YAML parse
check, and a Pydantic schema validation gate before atomic write.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml as _yaml
from ruamel.yaml import YAML

from tesseract.paths import TESSERACT_HOME, home_dir, home_logs_root, workspace_dir

log = logging.getLogger(__name__)


def workspace_events_dir() -> Path:
    """Resolve the workspace event store dir at call time. Honors a
    `TESSERACT_HOME` env override applied AFTER import (used by tests
    that point the kernel at a tmp_path)."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home_logs_root()

ProposalAction = Literal["append", "replace", "append_to_section"]

#: The sections of SOUL.md that grow, and what each one is for.
#:
#: One section per KIND of growth, so a lesson about how the assistant works
#: cannot displace something about who it is. A single shared list makes every
#: new observation compete with unrelated ones, and the file rotates instead of
#: accumulating.
#:
#: There is deliberately no per-section cap. A cap would have to evict
#: something the assistant learned in order to fire, and every bullet already
#: passes an operator approval before it lands: growth is gated by a person
#: rather than by a number.
#:
#: `purpose` is not decoration. It is what the propose tool shows the model to
#: decide where a bullet belongs. Ordered as they appear in the file.
SOUL_GROWTH_SECTIONS: dict[str, str] = {
    "Voice": "how I sound, and what lands with this person",
    "Craft": "working habits I have hardened, and mistakes I do not repeat",
    "What I care about": "taste and opinions I have actually formed, not inherited",
    "What I can do now": "capability I gained, that I did not have before",
    "Open questions": "what I am still unsure of, and want to find out",
}


#: The operator-owned documents, and how each may be changed.
#:
#: **Which files are on this list is decided in `permissions.yaml`**, under
#: `workspace_documents.documents`, because that is where the postures
#: protecting them are stated and a file on one list and not the other is a
#: file that is either unprotected or uneditable. The names are not read from
#: there at import — this module is imported early and a config read at import
#: time is a boot-order fault waiting to happen — so the two are tied by
#: `tests/workspace_doors_CC_12` instead, which fails on any difference in
#: either direction.
#:
#: What lives HERE and not there is the half that is not a permission: the
#: label a surface shows, and which actions the file accepts.
PROPOSABLE_PATHS: dict[str, dict[str, object]] = {
    "tesseract/workspace/SOUL.md": {
        "label": "Soul",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/USER.md": {
        "label": "User",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/OPERATING.md": {
        "label": "Operating",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/WORKSHOP.md": {
        "label": "Workshop",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/DIARY.md": {
        "label": "Diary",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    # Inlined into the prompt on every channel turn, and for a long time the
    # one workspace document no list named: not here, and not in the DENY
    # rules, so a `file_write` could edit it under a running backend. It is a
    # document like the others, and `scripts/generate_workspace.py` writing its
    # generated regions is no more special than the same script writing
    # OPERATING.md's.
    "tesseract/workspace/CHANNEL.md": {
        "label": "Channel",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
}

_WORKSPACE_KEY_PREFIX = "tesseract/workspace/"


def resolve_proposable_path(target_path: str) -> Path:
    """Resolve a `PROPOSABLE_PATHS` key to its call-time filesystem target.

    Keys (`tesseract/workspace/SOUL.md`, ...) are stable identifiers used
    across the event payload, the frontend, and tests — they never change.
    The filesystem target always lives under `workspace_dir()`
    (`<TESSERACT_HOME>/workspace`), resolved fresh on every call, so an app
    update that replaces the code tree never touches operator state. This
    is the single mapping point — callers must not re-derive this path.
    """
    return workspace_dir() / target_path[len(_WORKSPACE_KEY_PREFIX):]


_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)

# Per-target write locks. `apply_change` is a read → hash-check → write
# sequence, and every caller runs it on a worker thread
# (`asyncio.to_thread`), so two commits against the same document can
# genuinely interleave: both read the same `before`, both pass the drift
# check against the same hash, and both write. The `expected_hash_before`
# guard only settles a race where one write completes before the other
# reads — which was every race until an operator could commit directly
# alongside the approve path.
#
# A `threading.Lock` rather than an `asyncio.Lock` because this function is
# sync and is entered from worker threads, and it lives here rather than in
# a route so a caller cannot opt out by not knowing about it. Keyed on the
# resolved filesystem path, so the same document reached through different
# call sites contends. Never pruned — bounded by the PROPOSABLE_PATHS
# allowlist, and dropping a lock with a waiter would re-open the race.
_target_locks: dict[Path, threading.Lock] = {}
_target_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _target_locks_guard:
        lock = _target_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _target_locks[path] = lock
        return lock


def _atomic_replace(path: Path, text: str) -> None:
    """Write `text` over `path` via a UNIQUE sibling tempfile.

    The name has to be unique per call, not `<name>.tmp`: two writers
    racing on one deterministic tempfile truncate each other mid-write, and
    the bytes that land are whichever `replace` ran last — which is not
    necessarily the content either caller was told it committed.
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        # `newline=""` disables Windows \n -> \r\n translation. Without it a
        # one-paragraph edit through Identity -> Documents rewrote every line
        # in the file, because the text arrives with LF and Python's text mode
        # writes the platform's ending. The live workspace is LF and
        # `_shipping/` is CRLF, and whichever a file has is the one it keeps.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ProposeError(ValueError):
    """Raised when a propose request is malformed (bad path, action, or
    section). Surfaced to the assistant as a tool error so it can adjust."""


def growth_section_names() -> tuple[str, ...]:
    return tuple(SOUL_GROWTH_SECTIONS)


def validate_growth_section(section: str) -> str:
    """Return `section` if it is a declared growth section, else raise.

    A free-text section would create a new heading on approval, and the file
    would grow headings nobody reads instead of bullets somebody does.
    """
    if section not in SOUL_GROWTH_SECTIONS:
        raise ProposeError(
            f"unknown soul section {section!r} — pick one of: "
            + ", ".join(growth_section_names())
        )
    return section


class ConcurrentModificationError(RuntimeError):
    """Raised by `apply_change` when the on-disk hash differs from
    `expected_hash_before`. Caller (REST handler) returns 409 to the
    operator with the fresh diff so they can re-review."""

    def __init__(self, *, expected: str, actual: str, target: str) -> None:
        super().__init__(
            f"file changed since proposal: target={target} "
            f"expected={expected[:12]} got={actual[:12]}"
        )
        self.expected = expected
        self.actual = actual
        self.target = target


@dataclass(frozen=True)
class ChangeApplied:
    target_path: str
    action: ProposalAction
    bytes_before: int
    bytes_after: int
    hash_before: str
    hash_after: str
    # Set when apply_change returned without writing because the proposed
    # content was a duplicate of what's already in the target. The REST
    # commit handler surfaces this back so the operator gets a "duplicate,
    # no-op" toast instead of a silent success.
    no_op_reason: str | None = None


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_target(repo_root: Path, target_path: str) -> Path:
    """Resolve `target_path` and enforce the PROPOSABLE allowlist.

    `repo_root` is accepted for call-site compatibility but no longer
    determines the resolution — every PROPOSABLE_PATHS key is a workspace
    file, and workspace files always resolve under `workspace_dir()`
    (see `resolve_proposable_path`) so an app update that replaces the
    code tree never touches them.
    """
    target_path = (target_path or "").strip().replace("\\", "/")
    if not target_path:
        raise ProposeError("target_path is required")
    if target_path not in PROPOSABLE_PATHS:
        raise ProposeError(
            f"target_path {target_path!r} not in PROPOSABLE_PATHS — "
            f"only operator-owned workspace files can be proposed"
        )
    candidate = resolve_proposable_path(target_path)
    if not candidate.exists():
        raise ProposeError(f"target file does not exist: {target_path}")
    return candidate


def validate_action(target_path: str, action: str) -> ProposalAction:
    spec = PROPOSABLE_PATHS.get(target_path)
    if spec is None:
        raise ProposeError(f"target_path {target_path!r} not proposable")
    allowed = spec["allowed_actions"]
    if action not in allowed:  # type: ignore[operator]
        raise ProposeError(
            f"action {action!r} not allowed for {target_path}; "
            f"allowed={list(allowed)}"  # type: ignore[arg-type]
        )
    return action  # type: ignore[return-value]


def compute_diff(before: str, after: str, *, target_label: str = "file") -> str:
    """Unified diff with stable headers (no per-run timestamps in the diff
    body — keeps event payloads stable for golden tests)."""
    diff_iter = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{target_label} (current)",
        tofile=f"{target_label} (proposed)",
        n=3,
    )
    return "".join(diff_iter)


def preview_change(
    *,
    current_text: str,
    action: ProposalAction,
    content: str,
    section: str | None = None,
) -> str:
    """Compute the proposed `after` text without writing. Same logic as
    `apply_change` — kept separate so the propose tool can build a diff
    preview without owning the destination file."""
    if action == "append":
        if current_text and not current_text.endswith("\n"):
            return current_text + "\n" + content
        return current_text + content
    if action == "replace":
        return content
    if action == "append_to_section":
        if not section:
            raise ProposeError("append_to_section requires `section`")
        return _append_to_named_section(current_text, section, content)
    raise ProposeError(f"unknown action: {action}")


def document_posture(context: Any) -> str:
    """What a proposed change to an operator-owned document does right now.

    One reader, called by every tool that proposes one. Both propose tools
    needed the same answer about the same file and deriving it twice is how a
    document ends up auto for one caller and gated for the other.

    It asks the LIVE policy rather than re-reading `permissions.yaml`, because
    `/mode` changes the mode in memory and leaves the file alone: a call-time
    file read would answer with the mode the operator switched away from.

    No policy wired means no operator is reachable either, and the half that
    waits for one is the safe half.
    """
    policy = getattr(context, "policy", None)
    if policy is None:
        return "ask"
    try:
        return policy.workspace_document_posture()
    except AttributeError:
        # A stub policy with no such method: unit fixtures, and nothing else.
        # `ValueError` is deliberately NOT caught. It means the block does not
        # name the running mode, which the loader refuses at boot and `reload`
        # refuses before it mutates anything, so a policy that can raise it
        # cannot be built here. Swallowing it would turn a condition that
        # cannot happen into an approval card nobody could explain.
        return "ask"


def settle_proposal(
    *,
    event: Any,
    target_path: str,
    action: ProposalAction,
    content: str,
    section: str | None,
    expected_hash_before: str,
    posture: str,
) -> tuple[Any, "ChangeApplied | None", str | None]:
    """File a proposal, and apply it first when the posture says to.

    The one door. Returns `(event, applied, error)`.

    - **ask** — the event is filed pending and nothing is written. The
      operator settles it in the workspace inbox, which is the surface every
      approval on this runtime already uses.
    - **auto** — the change is applied and the SAME event is filed already
      decided. That is the half worth being careful about: flipping to
      unattended must cost speed and not visibility, so the card still
      carries the full diff, both hashes and the drift token. An operator
      reading the inbox afterwards sees exactly what an approved change would
      have shown them, marked `applied` rather than `approved`.
    - **deny** — refused, and the caller says so.

    The write goes through `apply_change`, so the auto path takes the same
    per-target lock, the same drift check and the same head-revision bump the
    approve path takes. There is no second write here, which is the whole
    reason this function exists rather than a branch in each tool.
    """
    posture = (posture or "ask").strip().lower()
    if posture == "deny":
        return event, None, (
            f"changes to {target_path} are refused by the current security "
            f"mode; nothing was written and nothing was filed"
        )
    if posture != "auto":
        # Already `pending`; returned unchanged so the caller has one shape to
        # append whichever branch ran.
        return event, None, None

    try:
        applied = apply_change(
            repo_root=workspace_dir(),
            target_path=target_path,
            action=action,
            content=content,
            section=section,
            expected_hash_before=expected_hash_before,
        )
    except ConcurrentModificationError as exc:
        return event, None, (
            f"{target_path} changed while this was being prepared, so nothing "
            f"was written. Read it again and propose against the new text: {exc}"
        )
    except ProposeError as exc:
        return event, None, str(exc)
    except OSError as exc:
        return event, None, f"could not write {target_path}: {exc}"

    _journal_applied(target_path=target_path, action=action, applied=applied)
    return (
        event.with_status("applied", reason="applied without asking: the mode says so"),
        applied,
        None,
    )


#: How each action reads once it has happened. The journal is prose an operator
#: reads, not a field they decode.
_PAST: dict[str, str] = {
    "append": "added to",
    "replace": "rewritten",
    "append_to_section": "added to",
}


def _journal_applied(
    *, target_path: str, action: ProposalAction, applied: ChangeApplied
) -> None:
    """One line in the operator journal for a change made without being asked.

    The workspace event already carries the diff and both hashes, and the inbox
    already renders it. What the inbox cannot answer is the operator's actual
    question: *"in any case, we can track all changes from autonomy?"* The
    Autonomy panel's Journal room is where every decision taken without them is
    read, and it is reachable from a phone through `autonomy_read`, so the
    answer belongs there rather than in a second room over the same events.

    `approval` because that is what this is. The mode approved it in their
    place, and a row saying anything else would describe the mechanism instead
    of what happened.

    **A no-op is not journalled.** `apply_change` returns without writing when
    the proposed content is already in the target, and a line saying a file was
    rewritten when its bytes never moved is the same defect in the journal that
    `no_op_reason` exists to prevent in the toast.

    Best-effort by the journal's own contract: it logs and swallows a write
    error. A file that has already been changed must never be reported as
    refused because the note about it could not be filed.
    """
    if applied.no_op_reason:
        return

    try:
        from tesseract.orchestrator.autonomy import journal as operator_journal

        operator_journal.append(
            "approval",
            {
                "summary": (
                    f"{target_path} was {_PAST.get(action, 'changed')} without asking"
                ),
                "by": "mode",
                "target_path": target_path,
                "action": action,
                "hash_before": applied.hash_before,
                "hash_after": applied.hash_after,
            },
        )
    except Exception:
        # The write already happened. Letting anything from here reach the
        # caller would report a file that WAS changed as refused, and the
        # operator would be told to propose again against text that has
        # already moved. The journal swallows its own OSError; this catches
        # everything else, including an import that fails.
        log.exception("workspace change applied but not journalled: %s", target_path)


def apply_change(
    *,
    repo_root: Path,
    target_path: str,
    action: ProposalAction,
    content: str,
    section: str | None = None,
    expected_hash_before: str | None = None,
) -> ChangeApplied:
    """Atomically apply the change. Raises ConcurrentModificationError if
    the file changed since propose-time (when `expected_hash_before` is
    given).

    The whole read → drift-check → write span holds a per-target lock, so
    the loser of a race re-reads and raises rather than committing on top
    of a hash it already lost. See `_target_locks`."""
    full_path = validate_target(repo_root, target_path)
    action = validate_action(target_path, action)

    # Imported before the lock: the first call in a process pays for loading
    # `tesseract.brain.prompt`, and paying for it while holding a write lock
    # would block a concurrent writer on an import that has nothing to do with
    # the file. Lazily here rather than at module scope because this is kernel
    # code and reaching into brain at import time would invert the dependency.
    from tesseract.brain.prompt import bump_head_revision

    with _lock_for(full_path):
        applied = _apply_change_locked(
            full_path=full_path,
            target_path=target_path,
            action=action,
            content=content,
            section=section,
            expected_hash_before=expected_hash_before,
        )
        # A conversation holds its head for its whole life, so an approved
        # edit to SOUL.md, USER.md or OPERATING.md would otherwise not be read
        # again until the next chat. This is the one funnel every approved
        # document write passes through, and the only place that KNOWS the
        # operator acted: further down, an approved edit and a background job
        # rewriting the same file are the same bytes.
        #
        # Inside the lock, with the write. Outside it there is a window where
        # the new bytes are readable but the revision retiring the old head has
        # not been published, so a turn assembling in that window snapshots
        # fresh content under a stale revision and re-reads once more than it
        # needed to. Never a swallowed edit, but free to close.
        #
        # Fired for every target, not only the three the head inlines. A list
        # here would be a second roster to keep in step with `prompt.SECTIONS`,
        # and the cost of being wrong in this direction is one re-read.
        #
        # Not fired for a no-op: `apply_change` short-circuits when the content
        # is already present, and retiring every conversation's head over bytes
        # that did not move is the exact waste this mechanism exists to stop.
        if applied.no_op_reason is None:
            bump_head_revision()
    return applied


def _apply_change_locked(
    *,
    full_path: Path,
    target_path: str,
    action: ProposalAction,
    content: str,
    section: str | None,
    expected_hash_before: str | None,
) -> ChangeApplied:
    # Three properties have to hold together here, and the order below is
    # what holds all of them at once:
    #
    #   1. No write ever lands on content the operator did not review, so the
    #      drift check precedes every call to `_atomic_replace`.
    #   2. A change that writes nothing is a success, not a conflict. A
    #      proposal whose text is already in the file has nothing for the
    #      drift check to protect, so it must not be refused because some
    #      other edit moved the hash. Running the drift check first made
    #      duplicate proposals permanently unapprovable: they 409'd on every
    #      Approve and the event stayed pending forever.
    #   3. An invalid proposal says so. `preview_change` now runs before the
    #      drift check, so a proposal that is both invalid AND drifted reports
    #      the invalidity, which is the one the operator has to fix first.
    before = full_path.read_text(encoding="utf-8")
    actual_hash = hash_text(before)

    after = preview_change(
        current_text=before,
        action=action,
        content=content,
        section=section,
    )
    if after != before and expected_hash_before and expected_hash_before != actual_hash:
        raise ConcurrentModificationError(
            expected=expected_hash_before,
            actual=actual_hash,
            target=target_path,
        )

    if after == before:
        # Idempotent commit. For `append_to_section` the only way to land
        # here is the bullet-dedup branch in `_append_to_named_section` —
        # tag it `"duplicate"` so the operator gets an accurate toast.
        # For `append`/`replace`, `after == before` means the proposed
        # text was byte-identical to current — tag it `"unchanged"` so
        # the toast doesn't mislabel a harmless re-apply as a duplicate.
        reason = "duplicate" if action == "append_to_section" else "unchanged"
        return ChangeApplied(
            target_path=target_path,
            action=action,
            bytes_before=len(before.encode("utf-8")),
            bytes_after=len(before.encode("utf-8")),
            hash_before=actual_hash,
            hash_after=actual_hash,
            no_op_reason=reason,
        )

    _atomic_replace(full_path, after)
    return ChangeApplied(
        target_path=target_path,
        action=action,
        bytes_before=len(before.encode("utf-8")),
        bytes_after=len(after.encode("utf-8")),
        hash_before=actual_hash,
        hash_after=hash_text(after),
    )


_BULLET_PREFIX_RE = re.compile(r"^\s*[-•*]\s+")


def _normalize_bullet(text: str) -> str:
    """Lowercase + strip leading bullet markers + collapse whitespace +
    drop trailing punctuation. Used for near-equivalence checks so two
    proposals of the same observation collapse to one bullet in a section.
    Mirrors the shape of `tesseract.memory.librarian._normalize` — kept
    inline to avoid a brain→kernel import."""
    stripped = _BULLET_PREFIX_RE.sub("", (text or "").strip())
    collapsed = re.sub(r"\s+", " ", stripped).lower()
    return collapsed.rstrip(".!?,;:")


def _section_contains_bullet(body: str, content: str) -> bool:
    """True when a normalized form of `content` matches an existing
    bullet line in `body`. Empty / placeholder bodies return False."""
    target = _normalize_bullet(content)
    if not target:
        return False
    for raw in body.splitlines():
        if not raw.strip():
            continue
        if _normalize_bullet(raw) == target:
            return True
    return False


# ─── YAML change-proposal apply path ───────────────────────────
#
# Separate from the markdown propose/apply path above. The inbox
# renderer dispatches yaml_change_proposal events to :func:`apply_yaml_change`
# which carries its own action vocabulary + pre-write checks (drift / parse /
# schema). Approve-vs-apply lifecycle: the operator approves a proposal, the
# REST decide handler calls this function, success → event status flips to
# "applied" and the file is mutated atomically. Failure → event stays "pending"
# with the reason surfaced on the row.

YamlChangeAction = Literal[
    "insert_under_path",
    "update_field",
    "append_to_list_at_path",
]


@dataclass(frozen=True)
class YamlChangeResult:
    ok: bool
    target_path: str
    action: str
    bytes_before: int = 0
    bytes_after: int = 0
    hash_before: str = ""
    hash_after: str = ""
    reason: str = ""  # populated when ok=False or no_op_reason is set
    no_op_reason: str | None = None


_YAML_CONFIG_KEY_PREFIX = "tesseract/config/"

_SUPPORTED_YAML_TARGETS: dict[str, str] = {
    # repo-relative → schema module attr name
    "tesseract/config/providers.yaml": "ProvidersConfig",
    "tesseract/config/roles.yaml": "RolesConfig",
}


def _navigate_yaml(doc: Any, path: str) -> tuple[Any, str, list[str]]:
    """Walk ``doc`` along the dotted ``path``.

    Returns ``(parent, leaf_key, walked)`` where ``parent`` is the
    container that holds the leaf (creating intermediate dicts only for
    ``insert_under_path``; the caller decides whether to create), and
    ``walked`` is the list of keys traversed so far for error messages.
    Raises ``KeyError`` when an intermediate key is missing.
    """
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise KeyError("yaml_path empty")
    cur = doc
    walked: list[str] = []
    for key in parts[:-1]:
        walked.append(key)
        if not isinstance(cur, dict):
            raise KeyError(f"yaml_path segment {key!r} traverses non-mapping at {'.'.join(walked[:-1]) or 'root'}")
        if key not in cur:
            raise KeyError(f"yaml_path segment {key!r} missing at {'.'.join(walked[:-1]) or 'root'}")
        cur = cur[key]
    return cur, parts[-1], walked


def _ruamel() -> YAML:
    ry = YAML()
    ry.preserve_quotes = True
    ry.indent(mapping=2, sequence=4, offset=2)
    return ry


def _apply_yaml_mutation(
    doc: Any,
    *,
    action: YamlChangeAction,
    yaml_path: str,
    content: Any,
) -> bool:
    """Mutate ``doc`` per ``action`` at ``yaml_path``. Returns True if a
    write should happen (False = no-op duplicate)."""
    parts = [p for p in yaml_path.split(".") if p]
    if not parts:
        raise KeyError("yaml_path empty")

    if action == "insert_under_path":
        # Navigate to the parent mapping (creating missing intermediate
        # dicts so a new model id slots into `api.<provider>.models.<id>`
        # even when the provider block was empty).
        cur = doc
        for key in parts[:-1]:
            if not isinstance(cur, dict):
                raise KeyError(f"yaml_path segment {key!r} traverses non-mapping")
            if key not in cur:
                cur[key] = {}
            cur = cur[key]
        leaf = parts[-1]
        if not isinstance(cur, dict):
            raise KeyError(f"yaml_path parent at {'.'.join(parts[:-1])} is not a mapping")
        if leaf in cur and cur[leaf] == content:
            return False
        cur[leaf] = content
        return True

    if action == "update_field":
        parent, leaf, _walked = _navigate_yaml(doc, yaml_path)
        if not isinstance(parent, dict):
            raise KeyError(f"yaml_path parent at {'.'.join(parts[:-1])} is not a mapping")
        if leaf in parent and parent[leaf] == content:
            return False
        parent[leaf] = content
        return True

    if action == "append_to_list_at_path":
        cur = doc
        for key in parts:
            if not isinstance(cur, dict):
                raise KeyError(f"yaml_path segment {key!r} traverses non-mapping")
            if key not in cur:
                raise KeyError(f"yaml_path segment {key!r} missing")
            cur = cur[key]
        if not isinstance(cur, list):
            raise KeyError(f"yaml_path target at {yaml_path} is not a list")
        if content in cur:
            return False
        cur.append(content)
        return True

    raise ValueError(f"unsupported action: {action}")


def _schema_for_target(target_path: str):
    """Return the Pydantic model class for the target, or None."""
    name = _SUPPORTED_YAML_TARGETS.get(target_path)
    if name is None:
        return None
    from tesseract.config import _schemas as schema_mod

    return getattr(schema_mod, name, None)


def apply_yaml_change(
    *,
    repo_root: Path,
    target_path: str,
    action: YamlChangeAction,
    yaml_path: str,
    content: Any,
    expected_hash_before: str,
) -> YamlChangeResult:
    """Atomically apply a YAML change proposal. See module docstring.

    Order of operations:

    1. Resolve + read the current file (under the call-time config dir,
       traversal guard).
    2. Drift check: ``sha256(current) == expected_hash_before``.
    3. Apply the mutation on a parsed ruamel doc (preserves comments + order).
       Duplicate writes short-circuit with ``no_op_reason='duplicate'``.
    4. Serialize → parse-check via ``yaml.safe_load`` (catches structural breakage).
    5. Schema validation via Pydantic (target-keyed).
    6. Atomic write via tempfile + ``os.replace``.

    Each failure returns a :class:`YamlChangeResult` with ``ok=False`` and a
    specific ``reason``. Never raises for the documented failure modes —
    the REST handler surfaces ``reason`` inline on the inbox row.

    ``repo_root`` is accepted for call-site compatibility but no longer
    determines the resolution — every ``_SUPPORTED_YAML_TARGETS`` key lives
    under ``tesseract/config/`` and always resolves under ``home_dir() /
    "config"`` (the same directory Task 4's kernel lockdown guards), so an
    app update that replaces the code tree never touches a pending or
    already-applied catalog edit. Resolving against ``repo_root`` (the code
    tree) would land writes outside the directory the runtime actually reads.
    """
    norm_target = (target_path or "").strip().replace("\\", "/")
    if not norm_target:
        return YamlChangeResult(ok=False, target_path=target_path, action=action, reason="target_path required")
    if not (norm_target.endswith(".yaml") or norm_target.endswith(".yml")):
        return YamlChangeResult(
            ok=False, target_path=target_path, action=action,
            reason=f"target_path {norm_target!r} is not a YAML file",
        )
    if not norm_target.startswith(_YAML_CONFIG_KEY_PREFIX):
        return YamlChangeResult(
            ok=False, target_path=target_path, action=action,
            reason=f"target_path {norm_target!r} not under {_YAML_CONFIG_KEY_PREFIX!r}",
        )
    config_dir = (home_dir() / "config").resolve()
    full = (config_dir / norm_target[len(_YAML_CONFIG_KEY_PREFIX):]).resolve()
    try:
        full.relative_to(config_dir)
    except ValueError:
        return YamlChangeResult(
            ok=False, target_path=target_path, action=action,
            reason=f"target escapes config dir: {norm_target!r}",
        )
    if not full.exists():
        return YamlChangeResult(
            ok=False, target_path=target_path, action=action,
            reason=f"target file does not exist: {norm_target}",
        )

    # Same read → drift-check → write span as `apply_change`, same lock.
    with _lock_for(full):
        return _apply_yaml_change_locked(
            full=full,
            norm_target=norm_target,
            action=action,
            yaml_path=yaml_path,
            content=content,
            expected_hash_before=expected_hash_before,
        )


def _apply_yaml_change_locked(
    *,
    full: Path,
    norm_target: str,
    action: YamlChangeAction,
    yaml_path: str,
    content: Any,
    expected_hash_before: str,
) -> YamlChangeResult:
    before_bytes = full.read_bytes()
    actual_hash = hashlib.sha256(before_bytes).hexdigest()
    if expected_hash_before and expected_hash_before != actual_hash:
        return YamlChangeResult(
            ok=False, target_path=norm_target, action=action,
            hash_before=actual_hash, reason="drift_detected",
        )

    ry = _ruamel()
    try:
        with full.open("r", encoding="utf-8") as fh:
            doc = ry.load(fh)
    except Exception as exc:  # noqa: BLE001
        return YamlChangeResult(
            ok=False, target_path=norm_target, action=action,
            hash_before=actual_hash, reason=f"yaml_load_failed: {exc}",
        )

    try:
        changed = _apply_yaml_mutation(doc, action=action, yaml_path=yaml_path, content=content)
    except (KeyError, ValueError) as exc:
        return YamlChangeResult(
            ok=False, target_path=norm_target, action=action,
            hash_before=actual_hash, reason=f"apply_failed: {exc}",
        )

    if not changed:
        return YamlChangeResult(
            ok=True, target_path=norm_target, action=action,
            bytes_before=len(before_bytes), bytes_after=len(before_bytes),
            hash_before=actual_hash, hash_after=actual_hash,
            no_op_reason="duplicate",
        )

    buf = io.StringIO()
    try:
        ry.dump(doc, buf)
    except Exception as exc:  # noqa: BLE001
        return YamlChangeResult(
            ok=False, target_path=norm_target, action=action,
            hash_before=actual_hash, reason=f"yaml_dump_failed: {exc}",
        )
    proposed_text = buf.getvalue()

    try:
        parsed_after = _yaml.safe_load(proposed_text)
    except Exception as exc:  # noqa: BLE001
        return YamlChangeResult(
            ok=False, target_path=norm_target, action=action,
            hash_before=actual_hash, reason=f"yaml_parse_failed: {exc}",
        )

    schema_cls = _schema_for_target(norm_target)
    if schema_cls is not None:
        try:
            schema_cls.model_validate(parsed_after)
        except Exception as exc:  # noqa: BLE001 — surface every pydantic failure mode
            return YamlChangeResult(
                ok=False, target_path=norm_target, action=action,
                hash_before=actual_hash, reason=f"schema_violation: {exc}",
            )

    proposed_bytes = proposed_text.encode("utf-8")
    try:
        _atomic_replace(full, proposed_text)
    except Exception as exc:  # noqa: BLE001
        return YamlChangeResult(
            ok=False, target_path=norm_target, action=action,
            hash_before=actual_hash, reason=f"write_failed: {exc}",
        )

    after_hash = hashlib.sha256(proposed_bytes).hexdigest()
    return YamlChangeResult(
        ok=True, target_path=norm_target, action=action,
        bytes_before=len(before_bytes), bytes_after=len(proposed_bytes),
        hash_before=actual_hash, hash_after=after_hash,
    )


def _append_to_named_section(text: str, section: str, content: str) -> str:
    """Insert `content` at the end of the `## {section}` block. Strips a
    placeholder italic line (`*Currently empty…*`) on first real entry —
    convention from SOUL.md's empty Growth section.

    Idempotent: if a normalized form of `content` already exists as a
    bullet in the section, returns `text` unchanged so `apply_change`
    short-circuits to a no-op. Catches both repeat operator approvals
    of the same `change_proposal` and distinct proposals carrying the
    same bullet text (e.g., two consolidator runs distilling the same
    pattern from the same active feedback set)."""
    heading_re = re.compile(rf"^## {re.escape(section)}\s*$", re.MULTILINE)
    m = heading_re.search(text)
    if m is None:
        raise ProposeError(
            f"section '## {section}' not found in target file — "
            f"refusing to add it silently"
        )
    section_start = m.end()
    next_h = _NEXT_HEADING_RE.search(text, section_start)
    section_end = next_h.start() if next_h else len(text)
    body = text[section_start:section_end]
    if _section_contains_bullet(body, content):
        return text
    placeholder_re = re.compile(r"\n\*Currently empty[^\n]*\*\n", re.IGNORECASE)
    if placeholder_re.search(body):
        body = placeholder_re.sub("\n", body)
    body = body.rstrip() + "\n"
    if not content.startswith("\n"):
        body += "\n"
    body += content
    if not body.endswith("\n"):
        body += "\n"
    return text[:section_start] + body + text[section_end:]
