"""Seeding of a relocated ``TESSERACT_HOME`` from the shipped templates.

On a packaged install the state trees start empty and the factory copies
live in the sealed app tree. Seeding is **additive**: every boot copies in
template files the install does not have, and records what it copied in
``runtime/seeded.json``.

That manifest is what lets "missing" and "deliberately deleted" be told
apart. Without it the only safe rule is "seed once, never again", which is
what the old sentinel gate did — and it meant a default added in a later
release never reached an existing install. The operator's copy always wins
over the template, and a file they delete stays deleted.

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


def load_seeded_keys() -> set[str]:
    """Config key paths this install has already had migrated in.

    Same contract as `load_seeded`, one level finer: the manifest is what
    lets "the operator never had this key" and "the operator deleted this
    key" be told apart. Entries look like
    ``providers.yaml::local.kokoro.download``.
    """
    keys = _load_manifest().get("keys")
    return {str(entry) for entry in keys} if isinstance(keys, list) else set()


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


def _write_manifest(
    *, paths: set[str], keys: set[str], digests: Mapping[str, str]
) -> None:
    """Written temp-then-rename so a power loss mid-write leaves the previous
    manifest intact rather than a half-written one."""
    from tesseract.paths import home_dir

    target = _manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "home": str(home_dir()),
            "paths": sorted(paths),
            "keys": sorted(keys),
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
    _write_manifest(
        paths=load_seeded() | added,
        keys=load_seeded_keys(),
        digests=load_seeded_digests(),
    )


def record_seeded_keys(keys: Iterable[str]) -> None:
    """Merge `keys` into the manifest's config-key list."""
    added = set(keys)
    if not added:
        return
    _write_manifest(
        paths=load_seeded(),
        keys=load_seeded_keys() | added,
        digests=load_seeded_digests(),
    )


def record_seeded_digests(digests: Mapping[str, str]) -> None:
    """Merge `digests` into the manifest's per-file digest map."""
    if not digests:
        return
    _write_manifest(
        paths=load_seeded(),
        keys=load_seeded_keys(),
        digests={**load_seeded_digests(), **digests},
    )


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    yaml has `migrate_config_keys` and binaries have no merge story at all.
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
    `migrate_config_keys` is the per-key answer to that; this is the markdown
    one, and it is deliberately narrower. Prose cannot be merged key-by-key,
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
        if key not in seeded or recorded is None or not target.is_file():
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
    migrate_config_keys()
    _stamp_born_at_if_empty()


def _missing_key_paths(
    template: Mapping, current: Mapping, parents: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Key paths present in `template` but absent from `current`.

    Recurses only where BOTH sides are mappings — that is what makes this a
    key merge rather than a value merge. A key the operator already has is
    never looked inside for a value comparison, so a knob they retuned is
    invisible to this function and cannot be reverted by it. A key whose
    value is a list is copied whole or not at all: merging into a list has
    no correct answer (append? prepend? dedupe?), and every list in these
    files is an ordered operator decision.

    Paths are tuples of key names, never a joined string. Config keys can
    contain dots themselves — `roles.yaml::voice.tts.settings` is keyed by
    catalog refs like `local.kokoro.af_heart` — so splitting a dotted path
    back into parts would address the wrong node and write into the wrong
    place.
    """
    found: list[tuple[str, ...]] = []
    for key, value in template.items():
        name = str(key)
        path = (*parents, name)
        if name not in current:
            found.append(path)
            continue
        existing = current[name]
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            found.extend(_missing_key_paths(value, existing, path))
    return found


def _set_key_path(doc: Any, path: tuple[str, ...], value: Any) -> None:
    for part in path[:-1]:
        doc = doc[part]
    doc[path[-1]] = value


def _template_value(template: Mapping, path: tuple[str, ...]) -> Any:
    value: Any = template
    for part in path:
        value = value[part]
    return value


def _manifest_key(filename: str, path: tuple[str, ...]) -> str:
    """The manifest entry for one migrated key.

    ``\\x1f`` (unit separator) joins the parts rather than ``.`` so a key
    containing a dot cannot collide with a nested path that spells the same
    string — the entry has to identify exactly one node to make a deletion
    stick to exactly one key.
    """
    return f"{filename}::" + "\x1f".join(path)


def migrate_config_keys() -> list[str]:
    """Add config keys the shipped templates gained since this install seeded.

    Seeding is per-FILE: a yaml the operator already has is never touched
    again, so a key added to a template in a later release never reaches an
    existing install. That is how an install that predates the Kokoro entry
    or the model download pins ends up running a config the runtime has
    outgrown, with no path forward short of deleting the file.

    This is the per-key half of the same contract, and it only ever ADDS.
    An existing key is left exactly as the operator left it, whatever its
    value — including `false`, `null`, and empty. Nothing is reordered, no
    comment is lost (ruamel round-trip), and a file that needs no keys is
    not rewritten at all.

    Deletions are respected the same way `seed_tree` respects a deleted
    file: every key added here is recorded in ``runtime/seeded.json``, so an
    operator who removes one afterwards keeps it removed. A key they deleted
    BEFORE this manifest existed returns once and then stays gone — the same
    one-time behaviour file-level seeding already has, rather than a second
    rule to reason about.

    Returns the key paths added, newest install state on disk.
    """
    import yaml
    from ruamel.yaml.error import YAMLError as RuamelError

    from tesseract.lib.yaml_io import load_round_trip, round_trip_yaml
    from tesseract.paths import TESSERACT_DIR, config_dir

    src = TESSERACT_DIR / "config"
    dest = config_dir()
    if dest.resolve() == src.resolve():
        return []  # dev: the operator's config IS the template

    already = load_seeded_keys()
    added: list[str] = []
    for template_path in sorted(src.glob("*.yaml")):
        target = dest / template_path.name
        if not target.exists():
            continue  # seeding owns whole files; nothing to merge into
        try:
            # Round-trip, not `safe_load`: the value inserted below is the
            # template's own node, so loading it this way is what carries the
            # block's comments and quote styles across with it. A `kokoro:`
            # entry that arrives in an operator's file stripped of the prose
            # explaining what `mix` does is a worse config than one that never
            # arrived — these files exist to be read and hand-edited.
            template = load_round_trip(template_path)
            current = yaml.safe_load(target.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError, RuamelError):
            # A config the operator has broken is theirs to fix. Rewriting it
            # from a template would discard their work at the exact moment
            # they are least able to notice.
            continue
        if not isinstance(template, Mapping) or not isinstance(current, Mapping):
            continue

        pending = [
            path
            for path in _missing_key_paths(template, current)
            if _manifest_key(template_path.name, path) not in already
        ]
        if not pending:
            continue

        # Both defaults bind the loop variables into the closure: without
        # them every deferred call would see whatever the last iteration
        # left behind.
        def _apply(
            doc: Any,
            pending: list[tuple[str, ...]] = pending,
            template: Mapping = template,
        ) -> None:
            for path in pending:
                _set_key_path(doc, path, _template_value(template, path))

        round_trip_yaml(target, _apply)
        added.extend(_manifest_key(template_path.name, path) for path in pending)

    record_seeded_keys(added)
    return added


def _stamp_born_at_if_empty() -> None:
    """The shipped identity.yaml carries ``born_at: ""`` so no operator
    timezone leaks into the template. Stamp the instance's actual birth time
    once so the prompt's "Age: day N" line isn't blank forever.

    Idempotent, and called on every boot rather than from a one-time seed
    branch — under additive seeding there is no single fresh-seed moment."""
    import yaml

    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import config_dir

    identity_path = config_dir() / "identity.yaml"
    if not identity_path.exists():
        return
    current = yaml.safe_load(identity_path.read_text(encoding="utf-8")) or {}
    if current.get("born_at"):
        return  # already set — never overwrite

    from datetime import datetime

    now_iso = datetime.now().astimezone().isoformat()
    round_trip_yaml(identity_path, lambda doc: doc.__setitem__("born_at", now_iso))


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


def ensure_agents_seeded() -> None:
    from tesseract.paths import TESSERACT_DIR, agents_dir

    src = TESSERACT_DIR / "agents"
    if not src.exists():
        return
    dest = agents_dir()
    if dest.resolve() == src.resolve() or dest.exists():
        return
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyc"))
