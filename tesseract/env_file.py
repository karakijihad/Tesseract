"""Read and write the operator's ``<TESSERACT_HOME>/.env``.

Reading and writing are ``python-dotenv``'s job, not ours: it is already the
dependency that loads this file at boot, so its parser is the only one whose
idea of a line, a quote or an ``export`` prefix can never disagree with what
the app actually runs on. ``dotenv_values`` reads, ``set_key``/``unset_key``
write, and both preserve the comments and section headers that make the file
worth opening by hand.

What is ours is the part dotenv has no opinion about: ``.env.example`` is a
*document* as well as a template — a caption per section, prose per key, a
signup URL — and the settings view is rendered from it so the operator sees
what a key unlocks and where to get one. That parsing lives here.

Two presence flags, never a value:

* ``in_file`` — the key has a non-empty value in ``.env``.
* ``active``  — the key is in this process's environment.

They disagree for exactly as long as it takes to restart, which is what makes
the restart requirement legible instead of a warning nobody connects to their
own edit: ``.env`` is read once at boot (``mirror/server/app.py`` calls
``load_dotenv`` during startup) while the rest of ``config/`` is hot-reloaded
by the watcher.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from dotenv import dotenv_values, set_key, unset_key

# `# ── 1. Chat — the one key that ... ──────` in `.env.example`. The number
# and the prose are one caption; the box-drawing rules around it are not.
_SECTION_RE = re.compile(r"^#\s*[─-]{2,}\s*(.+?)\s*[─-]{2,}\s*$")
# Only for reading `.env.example`'s own layout, which is written by this repo
# and always tight. Values in a real `.env` are never matched with this —
# dotenv's parser does that, and it accepts forms this deliberately does not.
_TEMPLATE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=")
_URL_RE = re.compile(r"https?://\S+")

# `${NAME}` in a value is POSIX variable expansion to dotenv, which would
# silently rewrite a key that happens to contain it. Nothing here wants that
# — this file holds credentials, not shell — so every read and the boot-time
# load turn interpolation off, and a value means itself.
INTERPOLATE = False

# Section 7 of `.env.example`. Its keys are not signup keys — a log level and
# four bearer tokens the operator generates — so the view groups them apart
# from the ones that start with a visit to a provider's website.
_ADVANCED_SECTION_PREFIX = "7."

# How many bytes of entropy a generated MCP bearer token carries. 32 bytes is
# the `secrets` module's own documented floor for a token nobody should be
# able to guess, and these tokens gate `trust_tier: operator`.
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class EnvKeySpec:
    """One key as ``.env.example`` describes it."""

    name: str
    section: str
    description: str
    signup_url: str | None
    advanced: bool


@dataclass(frozen=True)
class EnvKeyState:
    """One key as it stands right now. Never carries the value."""

    spec: EnvKeySpec
    in_file: bool
    active: bool


def example_path() -> Path:
    """The shipped template. Resolved at call time — an app update replaces
    the code tree under a live process."""
    from tesseract.paths import TESSERACT_DIR

    return TESSERACT_DIR / ".env.example"


def env_path() -> Path:
    from tesseract.paths import home_dir

    return home_dir() / ".env"


def _describe(comments: list[str]) -> tuple[str, str | None]:
    """Fold the comment lines above a key into (prose, signup_url).

    A line that is nothing but a URL contributes only the URL; a line that
    carries a URL *and* prose contributes both, because ``XAI_API_KEY``'s
    entry says what the key also arms on the same line as its console link.
    """
    url: str | None = None
    prose: list[str] = []
    for line in comments:
        match = _URL_RE.search(line)
        if match:
            if url is None:
                # `(https://t.me/BotFather)` in the template would otherwise
                # render — and open — with the closing bracket attached.
                url = match.group(0).rstrip(".,;)")
            remainder = (line[: match.start()] + line[match.end() :]).strip()
            remainder = remainder.lstrip("—-–").strip()
            if remainder:
                prose.append(remainder)
            continue
        prose.append(line)
    return " ".join(part for part in prose if part).strip(), url


def parse_example(path: Path | None = None) -> list[EnvKeySpec]:
    """Every key the template declares, in the order it declares them."""
    target = path or example_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    specs: list[EnvKeySpec] = []
    section = ""
    comments: list[str] = []
    # A run of keys written back to back under one caption — the Telegram
    # pair, the four MCP tokens. The caption describes all of them, and
    # attributing it only to the first would leave the rest rendering as a
    # name with nothing beside it, which is the state this view exists to
    # end. Broken by any blank line, comment or section header.
    run: tuple[str, str | None] | None = None
    for line in lines:
        stripped = line.strip()
        section_match = _SECTION_RE.match(stripped)
        if section_match:
            section = section_match.group(1).strip()
            comments = []
            run = None
            continue
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
            run = None
            continue
        key_match = _TEMPLATE_KEY_RE.match(stripped)
        if key_match:
            if comments:
                description, url = _describe(comments)
            elif run is not None:
                description, url = run
            else:
                description, url = "", None
            specs.append(
                EnvKeySpec(
                    name=key_match.group(1),
                    section=section,
                    description=description,
                    signup_url=url,
                    advanced=section.startswith(_ADVANCED_SECTION_PREFIX),
                )
            )
            comments = []
            run = (description, url)
            continue
        comments = []
        run = None
    return specs


def read_values(path: Path | None = None) -> dict[str, str]:
    """Every ``NAME=value`` pair in ``.env``, values included.

    The one function in this module that returns secrets. Callers that answer
    an HTTP request want :func:`states` instead — nothing outside this process
    needs a value back, and the route layer never has one to leak.
    """
    target = path or env_path()
    if not target.is_file():
        return {}
    return {
        name: value or ""
        for name, value in dotenv_values(target, interpolate=INTERPOLATE).items()
    }


def states(
    specs: Iterable[EnvKeySpec] | None = None,
    *,
    path: Path | None = None,
) -> list[EnvKeyState]:
    """Presence of every templated key, in file and in this process."""
    resolved = list(specs) if specs is not None else parse_example()
    values = read_values(path)
    return [
        EnvKeyState(
            spec=spec,
            in_file=bool(values.get(spec.name, "").strip()),
            active=bool((os.environ.get(spec.name) or "").strip()),
        )
        for spec in resolved
    ]


def set_values(updates: Mapping[str, str], *, path: Path | None = None) -> list[str]:
    """Write ``updates`` into ``.env``. Returns the names written.

    One key at a time through ``set_key``, so a failure on one cannot cost
    the others — a first run submitting five keys must not lose four of them
    to the fifth. An empty value removes the key's line via ``unset_key``;
    the settings view lists keys from the template rather than from this
    file, so a removed line still shows up there as "not set".
    """
    target = path or env_path()
    if not target.exists():
        if path is None:
            # Seed before writing, never after. `ensure_env_seeded` copies the
            # documented template only when the file is absent, so creating a
            # bare one here first would cost the operator every comment,
            # section caption and signup link in it — permanently, since the
            # seeder is first-run-only. Enforced here rather than remembered
            # by each caller.
            from tesseract.config_seed import ensure_env_seeded

            ensure_env_seeded()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)

    written: list[str] = []
    for name, value in updates.items():
        if value == "":
            unset_key(target, name)
        else:
            set_key(target, name, value)
        written.append(name)
    return sorted(written)


def generate_token() -> str:
    """A fresh bearer token for one ``mcp.yaml`` client."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


__all__ = [
    "EnvKeySpec",
    "EnvKeyState",
    "INTERPOLATE",
    "example_path",
    "env_path",
    "parse_example",
    "read_values",
    "states",
    "set_values",
    "generate_token",
]
