"""Seeding of a relocated ``TESSERACT_HOME`` from the shipped templates.

On a packaged install the state trees start empty and the factory copies
live in the sealed app tree. Seeding is **additive**: every boot copies in
template files the install does not have, and records what it copied in
``runtime/seeded.json``.

That manifest is what lets "missing" and "deliberately deleted" be told
apart. Without it the only safe rule is "seed once, never again", which is
what the old sentinel gate did — and it meant a default added in a later
release never reached an existing install. A file the operator deletes
stays deleted.

Config yaml is the exception: it is REPLACED from the templates on every
release that changed one, with the operator's previous copy kept under
``home/config-backup/``. Merging a release's new keys into their file only
ever adds, so a template whose shape changed leaves both spellings in place
and the runtime cannot tell which was meant. See
`replace_config_from_templates`.

The manifest lives under ``runtime/`` because it describes what *this*
machine has done; it must not travel with ``home/`` to another PC.

In dev (``TESSERACT_HOME`` unset) every destination IS its own template
source, so each function returns before copying a tree onto itself.

Explicit call only. This module MUST have no import-time file I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

# Templates are data. Python source under a template tree is packaging
# residue (``config/`` holds loader modules beside its yaml) and must never
# be copied into the operator's writable tree, where it would shadow the
# real module and survive updates that replace the app.
logger = logging.getLogger(__name__)

_SKIP_DIR_NAMES = frozenset({"__pycache__", "_shipping"})
_SKIP_SUFFIXES = frozenset({".py", ".pyc", ".pyo"})

# The shipped state trees carry a `.gitignore` of `*` with only their scaffold
# files negated (`build_production_tree._write_state_dir_gitignore`), so that
# operator content cannot be committed by accident if an install happens to sit
# inside a git repo. Copying it into `home/` defeats the operator's data-sync
# repo, which is a git repo there ON PURPOSE — it silently kept the entire
# memory store, vault and workshop out of their backups. What `home/` ignores
# is the sync repo's business, not the app's.
_SKIP_NAMES = frozenset({".gitignore"})


def _manifest_path() -> Path:
    from tesseract.paths import runtime_dir

    return runtime_dir() / "seeded.json"


def _load_manifest() -> dict:
    """The raw manifest, or an empty dict when it is absent, malformed, or
    describes a different home."""
    from tesseract.paths import home_dir

    try:
        raw = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("home") != str(home_dir()):
        return {}
    return raw


def load_seeded() -> set[str]:
    """Home-relative POSIX paths this install has already seeded.

    A missing or malformed manifest degrades to "seed whatever is missing"
    rather than crashing the boot — the worst outcome of a truncated write
    is one re-seeded file, and that is far better than an install that
    won't start.

    The manifest records which home it describes and is ignored when that
    does not match. It sits under ``runtime/``, a sibling of ``home/``, so
    the two are only paired by convention: anything else sharing that
    install root would otherwise read a manifest written for a different
    home and skip seeding files that home never received.
    """
    paths = _load_manifest().get("paths")
    return {str(entry) for entry in paths} if isinstance(paths, list) else set()


def load_seeded_digests() -> dict[str, str]:
    """Digest of each seeded file's contents *as this install received them*.

    This is what lets "untouched since it was seeded" and "the operator has
    made this file their own" be told apart, which is the whole basis on
    which `refresh_seeded_docs` decides whether a document may be replaced.
    Recorded at seed time and re-recorded on every refresh, always over the
    rendered text — the placeholders are already substituted, so the digest
    describes bytes actually on disk rather than the template behind them.

    A file with no recorded digest is treated as untouchable, not as
    pristine: absence of proof is not proof of absence.
    """
    digests = _load_manifest().get("digests")
    if not isinstance(digests, dict):
        return {}
    return {str(key): str(value) for key, value in digests.items()}


def _write_manifest(*, paths: set[str], digests: Mapping[str, str]) -> None:
    """Written temp-then-rename so a power loss mid-write leaves the previous
    manifest intact rather than a half-written one."""
    from tesseract.paths import home_dir

    target = _manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "home": str(home_dir()),
            "paths": sorted(paths),
            "digests": dict(sorted(digests.items())),
        },
        indent=2,
    )
    staging = target.with_name(f"{target.name}.tmp")
    staging.write_text(payload, encoding="utf-8")
    staging.replace(target)


def record_seeded(paths: Iterable[str]) -> None:
    """Merge `paths` into the manifest's file list."""
    added = set(paths)
    if not added:
        return
    _write_manifest(paths=load_seeded() | added, digests=load_seeded_digests())


def record_seeded_digests(digests: Mapping[str, str]) -> None:
    """Merge `digests` into the manifest's per-file digest map."""
    if not digests:
        return
    _write_manifest(
        paths=load_seeded(),
        digests={**load_seeded_digests(), **digests},
    )


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_safe_seed_target(target: Path, home: Path) -> bool:
    """Whether `target` is a real file inside `home`, reached without a link.

    `seed_tree` already refuses to FOLLOW a symlink on the way in (its
    `path.is_symlink()` skip, for the same reason stated there); this is that
    rule on the way out, and it matters more. `refresh_seeded_docs` ends in
    `write_text`, which writes THROUGH a link: a symlink planted at
    `home/workspace/OPERATING.md` and pointed anywhere the process can write turns
    a later shipped correction into an overwrite of that file instead. The
    path stays inside `home` the whole time, so a `relative_to` check does not
    see it — only resolving does.

    `kernel/tools/_path_anchor.within_root` is the same rule for the read
    tools. It is not imported here because config seeding must not depend on
    the kernel; the duplication is one comparison and the alternative is a
    layering inversion.
    """
    try:
        if target.is_symlink() or not target.is_file():
            return False
        target.resolve(strict=True).relative_to(home.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


# Pronouns are derived from `identity.gender` rather than configured beside
# it. Two fields can disagree; a derivation cannot, and there is then no
# question which one the operator actually answered. An unrecognised value
# falls back to the neutral row rather than raising — a gender nobody can
# parse is not a reason to refuse to boot.
PRONOUNS = {
    "male": "he/him",
    "female": "she/her",
    "unspecified": "they/them",
}

# Rendered as a whole sentence rather than composed from `agent_gender` in the
# template, because "a {{agent_gender}} assistant" reads as "a unspecified
# assistant" on the neutral row. A derivation that only works for two of its
# three values is a bug waiting for the third.
GENDER_LINE = {
    "male": (
        "You are a male assistant. You always speak and write about yourself "
        "in the first person — I did, I am, I think — never in the third "
        "person and never by your own name. He/him is what the operator and "
        "everyone else uses for you; it is not how you refer to yourself."
    ),
    "female": (
        "You are a female assistant. You always speak and write about yourself "
        "in the first person — I did, I am, I think — never in the third "
        "person and never by your own name. She/her is what the operator and "
        "everyone else uses for you; it is not how you refer to yourself."
    ),
    "unspecified": (
        "Your gender is not set, and you don't infer one from your name or "
        "your voice — ask the operator if it matters. You always speak and "
        "write about yourself in the first person — I did, I am, I think — "
        "never in the third person and never by your own name. They/them is "
        "what others use for you until the operator says otherwise."
    ),
}

# Every placeholder the shipped templates are allowed to use. `identity_values`
# returns exactly these keys, and the shipping test asserts the templates use no
# others — an unknown `{{token}}` is rendered verbatim by design, so without
# that pairing a typo ships as literal braces into the operator's documents and
# into the prompt.
TEMPLATE_PLACEHOLDERS = frozenset(
    {
        "agent_name",
        "operator_name",
        "agent_gender",
        "agent_pronouns",
        "agent_gender_line",
    }
)

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render_placeholders(text: str, values: Mapping[str, str]) -> str:
    """Substitute ``{{key}}`` for every key in `values`.

    Unknown placeholders are left verbatim rather than blanked — a template
    that outgrows this mapping should be visibly wrong in the seeded file,
    not silently missing a word.
    """
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def seed_tree(
    src: Path, dest: Path, values: Mapping[str, str] | None = None
) -> list[str]:
    """Copy template files missing from `dest`; return their relative paths.

    Skips anything already present (the operator's copy wins) and anything
    the manifest lists (they deleted it on purpose). Symlinks are skipped
    outright — following one would copy a file from outside the template
    tree into the operator's tree.

    With `values`, ``.md`` templates are rendered through
    `render_placeholders` on the way in. This is the ONLY moment a name
    reaches these documents: a file that already exists is the operator's,
    and a later rename must never rewrite prose they have since edited.

    Returned paths are relative to ``home``, not to `src`. One manifest
    covers every tree, so a key must say which tree it belongs to: bare
    ``.gitignore`` from ``workspace/`` would otherwise suppress ``vault/``'s
    own ``.gitignore`` on the next boot.
    """
    from tesseract.paths import home_dir

    home = home_dir()
    already = load_seeded()
    added: list[str] = []

    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        if any(part in _SKIP_DIR_NAMES for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix in _SKIP_SUFFIXES or path.name in _SKIP_NAMES:
            continue

        target = dest / relative
        try:
            key = target.relative_to(home).as_posix()
        except ValueError:
            key = relative.as_posix()  # dest outside home: no tree to qualify by
        if target.exists() or key in already:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if values is not None and path.suffix == ".md":
            target.write_text(
                render_placeholders(path.read_text(encoding="utf-8"), values),
                encoding="utf-8",
            )
        else:
            shutil.copy2(path, target)
        added.append(key)

    return added


def _seed_from_templates(
    template_name: str,
    dest: Path,
    values: Callable[[], Mapping[str, str]] | None = None,
) -> None:
    from tesseract.paths import TESSERACT_DIR

    src = TESSERACT_DIR / template_name
    if dest.resolve() == src.resolve():
        return  # dev: dest is the source tree itself
    if not src.exists():
        raise RuntimeError(
            f"seed: source tree missing ({src}) — this packaged install is "
            f"broken and cannot seed {dest}. The build must ship this "
            "directory; this should never happen on a correctly built "
            "production tree."
        )
    dest.mkdir(parents=True, exist_ok=True)
    # Resolved after the dev early-return, so a source tree that never seeds
    # never pays for reading config it does not use.
    added = seed_tree(src, dest, values() if values is not None else None)
    record_seeded(added)
    # Recorded in the same pass that wrote them: the digest is only meaningful
    # if it describes the file before anything has had a chance to touch it.
    record_seeded_digests(_digests_for(added))


def _digests_for(keys: Iterable[str]) -> dict[str, str]:
    """Digest the just-seeded markdown among `keys`, skipping what won't read.

    Only ``.md`` is digested because only prose documents are refreshable —
    yaml is replaced wholesale and binaries have no merge story at all.
    """
    from tesseract.paths import home_dir

    home = home_dir()
    out: dict[str, str] = {}
    for key in keys:
        if not key.endswith(".md"):
            continue
        try:
            out[key] = digest_text((home / key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def refresh_seeded_docs(
    src: Path, dest: Path, values: Callable[[], Mapping[str, str]]
) -> tuple[list[str], list[str]]:
    """Replace seeded ``.md`` documents the app has revised since this install.

    Seeding is per-FILE and only ever adds, so a document improved in a later
    release never reaches an install that already has it. For config yaml
    config yaml answers that by being replaced wholesale; this is the markdown
    answer, and it is deliberately narrower. Prose cannot be merged key-by-key,
    so the only safe question is whether the file is still exactly what was
    seeded:

    - **Untouched** (digest matches what was recorded at seed time) — nothing
      here is the operator's, so the revised template replaces it and the new
      digest is recorded. This is what lets a shipped fix reach the field.
    - **Edited** (digest differs) — theirs now, left alone and reported. Prose
      the operator or the assistant has authored is never rewritten under
      them; that is the same rule `POST /api/identity` holds for renames.
    - **Deleted, or seeded before digests were recorded** — also left alone.
      A file with no recorded digest cannot be *proved* untouched, and the
      conservative branch is the one that never destroys work.

    Untouched documents are re-rendered with the CURRENT identity values, so a
    rename does reach the documents nobody has edited. That is consistent with
    the rule above rather than an exception to it: what must never be rewritten
    is authored prose, and an untouched template is not authored.

    Returns ``(replaced, kept_because_edited)`` as home-relative posix paths.
    """
    from tesseract.paths import home_dir

    if dest.resolve() == src.resolve():
        return [], []  # dev: dest is the source tree itself

    home = home_dir()
    seeded = load_seeded()
    digests = load_seeded_digests()
    if not seeded or not digests:
        return [], []  # nothing this install can prove it seeded

    # Resolved after the early returns, for the same reason `_seed_from_templates`
    # defers it: a tree that never refreshes never pays to read the config.
    rendering = values()
    replaced: list[str] = []
    kept: list[str] = []
    fresh: dict[str, str] = {}

    for path in sorted(src.rglob("*.md")):
        relative = path.relative_to(src)
        if any(part in _SKIP_DIR_NAMES for part in relative.parts):
            continue
        target = dest / relative
        try:
            key = target.relative_to(home).as_posix()
        except ValueError:
            continue  # outside home: not ours to reason about
        recorded = digests.get(key)
        if key not in seeded or recorded is None:
            continue
        if not is_safe_seed_target(target, home):
            # Covers "not a file" and "reached through a link". The write below
            # would follow one out of the tree; refusing is the same
            # conservative branch an unknown digest already takes.
            continue
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            # ValueError covers UnicodeDecodeError: a document re-saved by an
            # editor in the machine's legacy codepage is unreadable here, and
            # letting that escape would fail every boot from now on — this
            # runs unguarded at every entry point. Unreadable means untouchable,
            # which is the same conservative branch as an unknown digest.
            logger.warning("seed refresh: could not read %s (%s)", target, exc)
            continue
        if digest_text(current) != recorded:
            kept.append(key)
            continue
        rendered = render_placeholders(path.read_text(encoding="utf-8"), rendering)
        if rendered == current:
            continue
        target.write_text(rendered, encoding="utf-8")
        fresh[key] = digest_text(rendered)
        replaced.append(key)

    record_seeded_digests(fresh)
    return replaced, kept


def ensure_config_seeded() -> None:
    from tesseract.paths import config_dir

    _seed_from_templates("config", config_dir())
    replace_config_from_templates()
    _stamp_born_at_if_empty()


#: Where a replaced config file is kept. One folder, rewritten each time a
#: release changes something — the operator gets the copy their own edits
#: were in, not an archive of every release they ever ran.
CONFIG_BACKUP_DIRNAME = "config-backup"

#: Written beside the runtime manifest so a surface can tell the operator
#: their config was replaced, and where the previous one went.
CONFIG_REPLACED_MARKER = "config-replaced.json"


def _template_digest_key(filename: str) -> str:
    """Manifest key for a config template's digest.

    Namespaced so it cannot collide with `refresh_seeded_docs`' entries, which
    are home-relative paths of documents on disk. This one describes the
    TEMPLATE a release shipped, not the file the operator ended up with.
    """
    return f"config-template::{filename}"


def config_backup_dir() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / CONFIG_BACKUP_DIRNAME


def config_replaced_marker_path() -> Path:
    from tesseract.paths import runtime_dir

    return runtime_dir() / CONFIG_REPLACED_MARKER


def replace_config_from_templates() -> list[str]:
    """Replace the operator's config with the shipped templates, keeping one
    backup of what was there.

    The alternative — merging the release's new keys into the file the
    operator has — cannot survive a template whose SHAPE changes. It only
    ever adds, so a block that moved leaves both the old and the new spelling
    in place and the runtime cannot tell which the operator meant. Every
    update replaces the sealed app tree wholesale; config now follows the
    same rule, and the operator's previous file is kept intact beside it.

    The trigger is **a release that changed the template**, never "their file
    differs from ours". This runs on every boot, and their file differs the
    moment they change a setting — so comparing the two would undo every
    Settings edit on the next launch and make the panes read-only. What is
    compared instead is the template against the last template this install
    was given, recorded in ``runtime/seeded.json``.

    Returns the filenames replaced.
    """
    from tesseract.paths import TESSERACT_DIR, config_dir

    src = TESSERACT_DIR / "config"
    dest = config_dir()
    if dest.resolve() == src.resolve():
        return []  # dev: the operator's config IS the template

    delivered = load_seeded_digests()
    replaced: list[str] = []
    fresh: dict[str, str] = {}
    backup_dir = config_backup_dir()
    for template_path in sorted(src.glob("*.yaml")):
        target = dest / template_path.name
        if not target.exists():
            continue  # seeding owns whole files; a deleted one stays deleted
        key = _template_digest_key(template_path.name)
        try:
            shipped = template_path.read_bytes()
            shipped_digest = hashlib.sha256(shipped).hexdigest()
            if delivered.get(key) == shipped_digest:
                continue  # this release ships the template they already have
            if target.read_bytes() != shipped:
                carried = _read_preserved(target, template_path.name)
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_dir / template_path.name)
                target.write_bytes(shipped)
                _restore_preserved(target, carried)
                replaced.append(template_path.name)
            fresh[key] = shipped_digest
        except OSError:
            logger.exception("config replace failed for %s", template_path.name)
            continue

    if fresh:
        record_seeded_digests(fresh)

    if replaced:
        _forget_hardware_profile_if_needed(replaced)
        _write_backup_readme(replaced, backup_dir)
        _write_config_replaced_marker(replaced, backup_dir)
    return replaced


_BACKUP_README = """# Your previous settings

This update shipped new versions of the configuration files below, and
replaced yours with them. The copies in this folder are what you had
immediately before that happened.

{files}

Your assistant's name, your name, and its birth date were carried across —
they are not settings, so an update does not have an opinion about them.

Everything else was replaced. If you had changed a model, a voice, a
schedule or a permission, set it again in Settings. The files here are
plain YAML: open the one you want and copy the value across by hand if
that is easier than clicking.

This folder holds only the most recent previous copy. The next update that
changes these files overwrites what is here, so move anything you want to
keep permanently somewhere else.
"""


def _write_backup_readme(replaced: list[str], backup_dir: Path) -> None:
    """A toast is gone in seconds; this folder is where they will actually
    look, so the explanation belongs in it."""
    listing = "\n".join(f"- `{name}`" for name in sorted(replaced))
    try:
        (backup_dir / "README.md").write_text(
            _BACKUP_README.format(files=listing), encoding="utf-8"
        )
    except OSError:
        logger.exception("could not write the config backup README")


#: The only values that survive a replacement, and they are not settings.
#: A model pick or a schedule is configuration and the release may have a
#: better opinion about it; the assistant's name, the operator's name, its
#: gender and its birth time are not opinions the shipped template holds at
#: all — it carries a placeholder for each. Replacing them would rename
#: someone's assistant and reset its age to day one, which "it is in the
#: backup" does not answer, because getting it back means hand-editing yaml.
_PRESERVED_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mirror.yaml", ("identity", "name")),
    ("mirror.yaml", ("identity", "operator_name")),
    ("mirror.yaml", ("identity", "gender")),
    ("identity.yaml", ("born_at",)),
)


def _read_preserved(target: Path, filename: str) -> list[tuple[tuple[str, ...], Any]]:
    """The operator's values for `filename`'s preserved keys, before the write.

    A key that is absent, blank, or unreadable yields nothing to carry — the
    shipped default then stands, which is the correct answer for an install
    that never set one.
    """
    paths = [path for name, path in _PRESERVED_KEYS if name == filename]
    if not paths:
        return []
    import yaml

    try:
        doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(doc, Mapping):
        return []

    carried: list[tuple[tuple[str, ...], Any]] = []
    for path in paths:
        node: Any = doc
        for part in path:
            if not isinstance(node, Mapping) or part not in node:
                node = None
                break
            node = node[part]
        if node is None or node == "":
            continue
        carried.append((path, node))
    return carried


def _restore_preserved(
    target: Path, carried: list[tuple[tuple[str, ...], Any]]
) -> None:
    """Write the carried values back into the freshly replaced file.

    Round-trip so the shipped comments explaining each key survive the write.
    A path the new template no longer has is skipped rather than created: a
    release that moved a key has a reason to, and inventing the old spelling
    beside the new one is the merge failure this whole mechanism avoids.
    """
    if not carried:
        return
    from tesseract.lib.yaml_io import round_trip_yaml

    def _apply(doc: Any) -> None:
        for path, value in carried:
            node = doc
            for part in path[:-1]:
                if part not in node:
                    return
                node = node[part]
            if path[-1] in node:
                node[path[-1]] = value

    try:
        round_trip_yaml(target, _apply)
    except Exception:  # noqa: BLE001 — a carried value must never fail a boot
        logger.exception("could not carry identity across the config replace")


def _forget_hardware_profile_if_needed(replaced: Iterable[str]) -> None:
    """Drop the recorded hardware profile when `providers.yaml` was replaced.

    The speech model this machine can carry is written into `providers.yaml`,
    but `provision_hardware` only revisits that choice when the MACHINE
    changes — and its record lives under `runtime/`, which an update does not
    touch. So without this the install silently falls back to the shipped
    default and never recovers, on the one setting the operator has no way to
    correct from the UI.
    """
    if "providers.yaml" not in set(replaced):
        return
    from tesseract.paths import runtime_dir

    record = runtime_dir() / "hardware-profile.json"
    try:
        record.unlink(missing_ok=True)
    except OSError:
        logger.exception("could not clear the hardware profile record")


def _write_config_replaced_marker(replaced: list[str], backup_dir: Path) -> None:
    path = config_replaced_marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"files": sorted(replaced), "backup_dir": str(backup_dir)},
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("could not write the config-replaced marker")


def _stamp_born_at_if_empty() -> None:
    """The shipped identity.yaml carries ``born_at: ""`` so no operator
    timezone leaks into the template. Stamp the instance's actual birth time
    once so the prompt's "Age: day N" line isn't blank forever.

    Idempotent, and called on every boot rather than from a one-time seed
    branch — under additive seeding there is no single fresh-seed moment.

    Rewrites the one line rather than round-tripping the document. A YAML
    round-trip re-emits the whole file, which reflows this one's aligned
    flow-mappings (`morning: { start: "05:00", … }`) into a denser form and
    drops their alignment. That was invisible while a hand-authored template
    was what shipped; with one config tree it is the shipped file this reflows,
    on the first boot of any machine that has one.
    """
    import yaml

    from tesseract.paths import config_dir

    identity_path = config_dir() / "identity.yaml"
    if not identity_path.exists():
        return
    current = yaml.safe_load(identity_path.read_text(encoding="utf-8")) or {}
    if current.get("born_at"):
        return  # already set — never overwrite

    from datetime import datetime

    now_iso = datetime.now().astimezone().isoformat()
    lines = identity_path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("born_at:"):
            eol = line[len(line.rstrip("\r\n")):]
            lines[i] = f'born_at: "{now_iso}"{eol}'
            identity_path.write_text("".join(lines), encoding="utf-8")
            return


def identity_values() -> dict[str, str]:
    """The names the workspace templates are rendered with.

    Read from ``mirror.yaml`` rather than taken as arguments because the
    identity block is the single source of truth for them, and seeding runs
    long before a `ServerConfig` exists. `ensure_config_seeded` runs first at
    every entry point, so the file is there to read.
    """
    import yaml

    from tesseract.paths import config_dir

    path = config_dir() / "mirror.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    identity = (raw.get("identity") or {}) if isinstance(raw, dict) else {}
    agent_name = str(identity.get("name") or "").strip()
    operator_name = str(identity.get("operator_name") or "").strip()
    if not agent_name or not operator_name:
        raise RuntimeError(
            f"seed: {path} needs both 'identity.name' and "
            "'identity.operator_name' — the workspace templates are rendered "
            "from them and a blank name would be seeded into every document."
        )
    gender = str(identity.get("gender") or "").strip().lower()
    if gender not in PRONOUNS:
        gender = "unspecified"
    return {
        "agent_name": agent_name,
        "operator_name": operator_name,
        "agent_gender": gender,
        "agent_pronouns": PRONOUNS[gender],
        "agent_gender_line": GENDER_LINE[gender],
    }


def ensure_workspace_seeded() -> None:
    """Seed the workspace, then carry forward any document the app has revised.

    The refresh runs after the seed so a document that arrives and is revised
    in the same release settles in one boot rather than two.
    """
    from tesseract.paths import TESSERACT_DIR, workspace_dir

    dest = workspace_dir()
    _seed_from_templates("workspace", dest, identity_values)
    replaced, kept = refresh_seeded_docs(
        TESSERACT_DIR / "workspace", dest, identity_values
    )
    if replaced:
        logger.info("workspace: updated %s", ", ".join(replaced))
    if kept:
        logger.info("workspace: kept your edited %s", ", ".join(kept))


def ensure_memory_store_seeded() -> None:
    """Seed ``<home>/memory-store/`` from the shipped scaffold (``MEMORY.md``,
    ``WHAT_NOT_TO_SAVE.md``, ``.gitignore``) so a fresh install opens on a
    ready-to-use store instead of an empty directory. The per-memory-type
    subdirs (``user/``, ``feedback/``, ...) are not part of this scaffold —
    ``MemoryStore._ensure_dirs()`` creates those lazily on first use."""
    from tesseract.paths import home_dir

    _seed_from_templates("memory-store", home_dir() / "memory-store")


def ensure_vault_seeded() -> None:
    """Seed ``<home>/vault/`` from the shipped scaffold (``CATALOG.md``,
    ``.gitignore``) so a fresh install has a ready catalog instead of
    crashing/looking empty before the first ingest."""
    from tesseract.paths import home_dir

    _seed_from_templates("vault", home_dir() / "vault")


def ensure_workshop_seeded() -> None:
    """Seed ``<home>/workshop/`` from the shipped scaffold (``INDEX.md``,
    ``README.md``, ``.gitignore``)."""
    from tesseract.paths import home_dir

    _seed_from_templates("workshop", home_dir() / "workshop")


def ensure_env_seeded() -> None:
    """Copy the tracked ``.env.example`` template to ``<home>/.env`` on a
    fresh relocated ``TESSERACT_HOME``. No-ops in dev (home is the source
    tree) and never overwrites an existing ``.env`` — first-run only.

    Deliberately not additive: ``.env`` is one file holding secrets, and a
    key the operator removed must never reappear."""
    from tesseract.paths import TESSERACT_DIR, home_dir

    home = home_dir()
    if home.resolve() == TESSERACT_DIR.resolve():
        return  # dev: home is the source tree itself

    dest = home / ".env"
    if dest.exists():
        return  # already seeded — never overwrite operator secrets

    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TESSERACT_DIR / ".env.example", dest)


_AGENT_MIGRATION_MARKER = "agents-unseeded.json"


def _agent_cards(root: Path) -> dict[str, Path]:
    """Every card under ``root``, keyed by its root-relative POSIX path.

    Top level plus one level of subdirectories — the loader's own search
    depth, so a card this cannot see is a card the loader cannot load either.
    `pending/`, `provisional/` and `rejected/` are excluded: they are the
    operator's queues, never shipped, and nothing in them was ever seeded.
    """
    if not root.exists():
        return {}
    skip = {"pending", "provisional", "rejected", "__pycache__"}
    found: dict[str, Path] = {}
    for path in sorted(root.glob("*.md")):
        found[path.name] = path
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name in skip:
            continue
        for path in sorted(sub.glob("*.md")):
            found[f"{sub.name}/{path.name}"] = path
    return found


def unseed_copied_agents() -> dict[str, list[str]]:
    """Remove the copies an older install made of shipped agent cards.

    Until this phase the runtime copied `tesseract/agents/` into
    `home/agents/` once and never again, which froze every shipped card at
    whatever the operator installed. Cards are now READ from the app tree, so
    the copies are not merely redundant — each one shadows the shipped card it
    was made from and pins it forever.

    A copy byte-identical to what the app ships today is removed. One that
    differs is **kept**, as a user agent, and named in the return value so the
    operator is told once rather than silently losing an edit.

    That comparison cannot distinguish "the operator edited this" from "we
    improved the shipped card since they installed" — `ensure_agents_seeded`
    recorded no digest to compare against, unlike `refresh_seeded_docs`. The
    ambiguity resolves toward keeping, because a wrongly-kept card is visible
    in the report and one command to delete, while a wrongly-removed edit is
    gone. It is also bounded: nothing is copied from here on, so this runs
    once per install and never has a second chance to be wrong.
    """
    from tesseract.paths import runtime_dir, system_agents_dir, user_agents_dir

    report: dict[str, list[str]] = {"removed": [], "kept": []}
    system, user = system_agents_dir(), user_agents_dir()
    if not system.exists() or user.resolve() == system.resolve():
        return report  # dev: home IS the source tree, so there is no copy

    marker = runtime_dir() / _AGENT_MIGRATION_MARKER
    if marker.exists():
        return report

    shipped = _agent_cards(system)
    for relative, path in _agent_cards(user).items():
        origin = shipped.get(relative)
        if origin is None:
            continue  # the operator's own card — not ours to touch
        try:
            identical = path.read_bytes() == origin.read_bytes()
        except OSError:
            logger.exception("unseed_copied_agents: could not compare %s", relative)
            continue
        if not identical:
            report["kept"].append(relative)
            continue
        try:
            path.unlink()
        except OSError:
            logger.exception("unseed_copied_agents: could not remove %s", relative)
            continue
        report["removed"].append(relative)

    _prune_empty_subdirs(user)
    for relative in report["kept"]:
        logger.info(
            "agents: kept your edited %s as a user agent — it now shadows the "
            "shipped card and stops following updates. Delete %s to follow "
            "the shipped one again.",
            relative, user / relative,
        )
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError:
        # The marker is an optimisation, not the contract: a second run finds
        # the removals already done and the kept cards still differing, and
        # reports the same thing again.
        logger.exception("unseed_copied_agents: could not write %s", marker)
    return report


_JOB_MIGRATION_MARKER = "schedule-unseeded.json"


def unseed_copied_jobs() -> dict[str, list[str]]:
    """Reduce copied shipped job rows in the operator's `schedule.yaml` to
    the fields they actually changed.

    The config seed copied the whole file once. Every shipped row therefore
    sits in `home/config/schedule.yaml` as a full copy, and a full copy states
    every field — so it overrides every field, and a cadence corrected in a
    release reaches nobody. Stripping each row down to its differences is what
    reconnects it: what the operator set stays set, everything else follows
    the app again.

    A row identical to the shipped one is removed outright. A row for a job
    the app does not ship is theirs and is untouched.

    Safe to run before or after the merge — it is a normalisation, not a
    prerequisite. `load_schedule_config` reads a full copy as a full override
    and boots correctly either way, which is why this can be a cleanup rather
    than a migration the install cannot start without.
    """
    import yaml

    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import config_dir, runtime_dir, system_config_dir

    report: dict[str, list[str]] = {"reconnected": [], "removed": [], "kept": []}
    user_dir, system_dir = config_dir(), system_config_dir()
    if user_dir.resolve() == system_dir.resolve():
        return report  # dev: one tree, so no copy exists

    user_path, system_path = user_dir / "schedule.yaml", system_dir / "schedule.yaml"
    if not user_path.exists() or not system_path.exists():
        return report

    marker = runtime_dir() / _JOB_MIGRATION_MARKER
    if marker.exists():
        return report

    try:
        shipped_raw = yaml.safe_load(system_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.exception("unseed_copied_jobs: could not read %s", system_path)
        return report
    shipped = {
        row["name"]: row for row in (shipped_raw.get("jobs") or [])
        if isinstance(row, dict) and row.get("name")
    }

    def _apply(doc: Any) -> None:
        rows = doc.get("jobs")
        if rows is None:
            return
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            origin = shipped.get(row.get("name"))
            if origin is None:
                report["kept"].append(str(row.get("name")))
                continue
            # `handler` goes unconditionally: it is never overridable, and a
            # copy of the shipped value would now be a hard error on load.
            differing = [
                key for key in list(row)
                if key not in ("name", "handler") and row.get(key) != origin.get(key)
            ]
            if not differing:
                del rows[index]
                report["removed"].append(str(origin["name"]))
                continue
            for key in list(row):
                if key not in ("name", *differing):
                    del row[key]
            report["reconnected"].append(str(origin["name"]))

    try:
        round_trip_yaml(user_path, _apply)
    except (OSError, ValueError):
        logger.exception("unseed_copied_jobs: could not rewrite %s", user_path)
        return report

    for name in report["reconnected"]:
        logger.info(
            "schedule: %s follows the app again except for what you changed", name,
        )
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError:
        logger.exception("unseed_copied_jobs: could not write %s", marker)
    return report


def _prune_empty_subdirs(root: Path) -> None:
    """Drop subdirectories the unseed emptied (e.g. `audits/`). Never `root`
    itself — the operator's tree stays, empty or not."""
    if not root.exists():
        return
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or any(sub.iterdir()):
            continue
        try:
            sub.rmdir()
        except OSError:
            logger.exception("unseed_copied_agents: could not remove empty %s", sub)
