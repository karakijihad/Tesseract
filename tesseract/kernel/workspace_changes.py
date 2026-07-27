"""Generic propose/commit primitives for TARS-initiated workspace changes.

Mental model: TARS is a colleague sending change requests. Any mutation
of an operator-owned workspace `.md` file (SOUL.md, IDENTITY.md,
FOUNDATION.md, etc.) routes through this module:

    propose_change tool  ─►  WorkspaceEvent(kind="change_proposal")  ─►  inbox row
    operator clicks Approve in workspace  ─►  apply_change()  ─►  file mutated

No tool may write these files directly. The workspace `post_decision`
endpoint is the single commit point. Concurrency is guarded by an
`expected_hash_before` snapshot taken at propose time — if the file
mutates between propose and approve the commit fails with
`ConcurrentModificationError` and the operator re-reviews against the
fresh diff.

MO-10-2 extends this module with YAML-aware actions for catalog edits
proposed by the knowledge-keeper. Three new actions
(``insert_under_path`` / ``update_field`` / ``append_to_list_at_path``)
land via :func:`apply_yaml_change`, with a drift check, a YAML parse
check, and a Pydantic schema validation gate before atomic write.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml as _yaml
from ruamel.yaml import YAML

from tesseract.paths import TESSERACT_HOME, home_dir, workspace_dir


def workspace_events_dir() -> Path:
    """Resolve the workspace event store dir at call time. Honors a
    `TESSERACT_HOME` env override applied AFTER import (used by tests
    that point the kernel at a tmp_path)."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return home / "logs"

ProposalAction = Literal["append", "replace", "append_to_section"]

PROPOSABLE_PATHS: dict[str, dict[str, object]] = {
    "tesseract/workspace/SOUL.md": {
        "label": "Soul",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/IDENTITY.md": {
        "label": "Identity",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/FOUNDATION.md": {
        "label": "Foundation",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/USER.md": {
        "label": "User",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/VOICE.md": {
        "label": "Voice",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/AGENTS.md": {
        "label": "Agents",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/HEARTBEAT.md": {
        "label": "Heartbeat",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/MCP.md": {
        "label": "MCP",
        "allowed_actions": ("append", "replace", "append_to_section"),
    },
    "tesseract/workspace/TOOLS.md": {
        "label": "Tools",
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
    "tesseract/workspace/BOOT.md": {
        "label": "Boot",
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


class ProposeError(ValueError):
    """Raised when a propose request is malformed (bad path, action, or
    section). Surfaced to TARS as a tool error so it can adjust."""


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
    given)."""
    full_path = validate_target(repo_root, target_path)
    action = validate_action(target_path, action)

    before = full_path.read_text(encoding="utf-8")
    actual_hash = hash_text(before)
    if expected_hash_before and expected_hash_before != actual_hash:
        raise ConcurrentModificationError(
            expected=expected_hash_before,
            actual=actual_hash,
            target=target_path,
        )

    after = preview_change(
        current_text=before,
        action=action,
        content=content,
        section=section,
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

    tmp = full_path.with_suffix(full_path.suffix + ".tmp")
    tmp.write_text(after, encoding="utf-8")
    tmp.replace(full_path)
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


# ─── YAML change-proposal apply path (MO-10-2) ───────────────────────────
#
# Separate from the markdown propose/apply path above. The MO-10-2 inbox
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
    already-applied catalog edit. Distributable-app Phase 1, Task 5
    exit-gate finding: this used to resolve against ``repo_root`` (the code
    tree), landing writes outside the directory the runtime actually reads.
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
    tmp = full.with_suffix(full.suffix + ".tmp")
    try:
        tmp.write_bytes(proposed_bytes)
        os.replace(tmp, full)
    except Exception as exc:  # noqa: BLE001
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
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
