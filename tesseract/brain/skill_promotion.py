"""Move a quarantined skill into the active set, or archive a rejected one.

`create_skill` is the door a draft is written through, and this is the half
of the lifecycle that comes after a draft exists: a pending folder becomes
active, or moves to `rejected/`. `skill_door.py` re-exports both names.

A draft goes live through its card, or on its own where `skill_create`
resolves to `auto`, never through a tool call of its own. `promotion_is_auto`
asks the policy about `skill_create`: creating a skill and having it land
live are one decision, made at the one posture that governs it.

**Live means carried, on both paths.** `promote_pending_skill` is the one
function the door's own auto-promotion and the approved `skill_approval` card
both call, so it is the one place that adds the skill's name to
`carried.txt` (`brain/playbook_set.py::add_to_carried`). A carried-list write
failure is logged and never raised: the skill has already gone live by the
time this runs, and the promotion is not undone for a file it does not own.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from tesseract.brain.skills import (
    SKILL_PENDING_DIRNAME,
    SKILL_REJECTED_DIRNAME,
    SkillEntry,
    list_pending_skills,
    list_rejected_skills,
    list_skills_names,
    load_skill_folder,
)

logger = logging.getLogger(__name__)


def promotion_is_auto(registry: Any) -> bool:
    """Whether a drafted skill is promoted without a hand in the mode the
    install runs in.

    Asked of the same policy `skill_create` itself would be judged by, so a
    draft the runtime wrote goes live exactly where the operator has given
    the assistant that decision (`free`) and waits on the card exactly where
    they kept it (`max`, the shipped default). A registry with no policy
    attached, which is a test or a REPL, answers no: nothing is promoted on
    the strength of a missing file.
    """
    from pydantic import BaseModel

    from tesseract.kernel.tools.base import PermissionResult

    policy = getattr(registry, "permission_policy", None)
    if policy is None:
        return False

    class _NoInput(BaseModel):
        pass

    try:
        return policy.get_posture("skill_create", _NoInput()) is PermissionResult.PASSTHROUGH
    except Exception:  # noqa: BLE001 — a policy that cannot answer is a no
        return False


def promote_pending_skill(
    skills_dir: Path, name: str,
) -> tuple[SkillEntry | None, str | None]:
    """Move `pending/<name>/` into the active skills tree.

    The single promotion implementation shared by the door and the Workspace
    `skill_approval` card's approve route. Returns ``(entry, None)`` on
    success, ``(None, error)`` on failure.
    """
    if name not in list_pending_skills(skills_dir):
        pending = list_pending_skills(skills_dir)
        return None, (
            f"No pending skill {name!r} in {skills_dir / SKILL_PENDING_DIRNAME}. "
            f"Pending: {pending or '(none)'}"
        )

    if name in list_skills_names(skills_dir):
        return None, (
            f"Skill {name!r} already active. Remove it first if you want to "
            "replace it with the pending version."
        )

    src = skills_dir / SKILL_PENDING_DIRNAME / name
    # Validate the draft parses cleanly before moving anything — a malformed
    # pending skill should fail loudly here, not become an inert active folder.
    entry = load_skill_folder(src)
    if entry is None:
        return None, (
            f"Pending skill {name!r} failed validation (missing/oversize/"
            "invalid SKILL.md frontmatter). Fix the draft before promoting."
        )
    # Defense against a hand-placed pending folder whose frontmatter `name`
    # diverges from its dirname: the active manifest is keyed by frontmatter
    # name, so a mismatch would let a draft masquerade under the wrong slug.
    # The door enforces equality at draft time; enforce it again here.
    if entry.name != name:
        return None, (
            f"Pending skill folder {name!r} declares frontmatter name "
            f"{entry.name!r}; they must match. Fix the draft before promoting."
        )

    dst = skills_dir / name
    try:
        os.replace(str(src), str(dst))
    except OSError as exc:
        return None, f"Failed to promote {name!r}: {exc}"

    logger.info("Skill promoted: %s (%s)", name, dst)

    from tesseract.brain.playbook_set import CARRIED_FILENAME, add_to_carried

    carried_err = add_to_carried(name, skills_dir / CARRIED_FILENAME)
    if carried_err:
        logger.warning("skill promoted but not carried: %s", carried_err)

    return entry, None


def archive_rejected_skill(
    skills_dir: Path, name: str, reason: str | None,
) -> str | None:
    """Move `pending/<name>/` into `rejected/` and write a reason sidecar.

    Returns an error string on failure, None on success. Mirrors the agent
    reject path (`routes/workspace.py::_archive_rejected_agent`).
    """
    src = skills_dir / SKILL_PENDING_DIRNAME / name
    if not (src / "SKILL.md").exists():
        return f"No pending skill {name!r} to reject in {src}."

    rejected_dir = skills_dir / SKILL_REJECTED_DIRNAME
    rejected_dir.mkdir(parents=True, exist_ok=True)
    dst = rejected_dir / name
    # A prior rejection of the same name would block the move — clear it so
    # re-proposal + re-rejection stays idempotent (the reason sidecar below
    # records the latest decision).
    if dst.exists():
        import shutil

        shutil.rmtree(dst, ignore_errors=True)
    try:
        os.replace(str(src), str(dst))
    except OSError as exc:
        return f"Failed to archive rejected skill {name!r}: {exc}"

    if reason:
        try:
            (rejected_dir / f"{name}.reason.txt").write_text(
                reason.strip() + "\n", encoding="utf-8",
            )
        except OSError:
            logger.warning("skill reject: reason sidecar write failed for %s", name)
    return None
