"""Render the parts of the workspace documents that the code already owns.

The workspace is instructions — how she sounds, how she works, what she has
learned about the operator. None of that is derivable and none of it should be
templated. But a handful of paragraphs inside `OPERATING.md` state facts that
live in code or config: which prefixes a read falls back to, where the
embedding daemon answers, what the two gate refusals actually say, which fields
the temporal block carries. Those rot, quietly, in a document that is inlined
on every single turn.

So they are inserts. Each one is delimited in the document:

    <!-- generated: state-read-prefixes -->
    …rendered from `paths.READABLE_STATE_PREFIXES`…
    <!-- /generated -->

and everything outside the markers is authored prose that this script never
touches. `--check` fails if a region differs from what the code says; `--write`
brings it back. Both trees are rendered: the live workspace this checkout's
assistant reads, and the `_shipping/` copy every install is seeded from.

    python -m tesseract.scripts.generate_workspace --check
    python -m tesseract.scripts.generate_workspace --write

**What is deliberately NOT an insert.** `## Source of truth` names a handful of
config files with a gloss each, and the phase that scoped this work listed it as
code-owned. It is not: `tesseract/config/` holds far more YAML than that section
names, and which of them answer "what is actually wired" is an authored
judgement. Rendering them would mean carrying the list and the glosses inside
this script — the same authored fact, moved somewhere nobody reads. Generation
is for facts with an owner in code; this one has an owner in the operator.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path
from typing import Callable, NamedTuple

from tesseract.paths import ROOT, TESSERACT_DIR

_OPEN_PREFIX = "<!-- generated: "
_CLOSE = "<!-- /generated -->"

_MARKER = re.compile(
    r"(?P<open><!-- generated: (?P<name>[a-z0-9-]+) -->\r?\n)"
    r"(?P<body>.*?)"
    r"(?P<close>\r?\n<!-- /generated -->)",
    re.S,
)


def _read(path: Path) -> str:
    """The file as written, line endings included.

    `_shipping/` is CRLF and the live workspace is LF. Reading either through
    universal newlines and writing it back would rewrite every line in the
    file to report that one paragraph changed.
    """
    with io.open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


_SHELL_VAR = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*(?::-([^}]*))?\}$")


def _shipped_default(value: str) -> str:
    """The default half of `${VAR:-default}`, never the resolved env var.

    `resolve_env` is the runtime's reader and would bake whatever this machine
    exports into a document that ships. The generated text has to be the same
    on the operator's box and in CI, so it renders what a fresh install gets.
    """
    match = _SHELL_VAR.match(value or "")
    if match is None:
        return value
    default = match.group(1)
    if default is None:
        raise ValueError(
            f"{value!r} names an environment variable with no default. There is "
            "no portable value to write into a document that ships — give the "
            "config entry a default, or stop generating this sentence."
        )
    return default


def render_state_read_prefixes() -> str:
    """Where a bare relative read falls back to, after the code tree."""
    from tesseract.paths import READABLE_STATE_PREFIXES

    listed = ", ".join(f"`{prefix}/`" for prefix in READABLE_STATE_PREFIXES)
    return (
        f"That fallback is narrow. Only the prefixes where something the "
        f"runtime writes can land are tried — {listed} — and only after the "
        f"code tree has nothing."
    )


def render_memory_probe() -> str:
    """The endpoint to probe when semantic search is offline.

    Read from `providers.yaml` rather than written down. The document used to
    say `localhost`, which on Windows resolves to `::1` first and spends ~2s
    failing over to IPv4 before reaching a daemon that answers in 5ms — the
    exact mistake the config file's own comment exists to stop.
    """
    import yaml

    from tesseract.paths import config_dir

    raw = yaml.safe_load(
        (config_dir() / "providers.yaml").read_text(encoding="utf-8")
    )
    ollama = raw["local"]["ollama"]
    url = f"{_shipped_default(ollama['base_url']).rstrip('/')}{ollama['models_endpoint']}"
    return f"Probe it with `curl -sS {url}`."


def render_gate_outcomes() -> str:
    """The two shapes a gated call comes back in, quoted from the runtime.

    The QUOTES only. What to do about each is authored prose outside the
    region — a generated block that also carried the guidance would put
    instructions somewhere the operator cannot edit them, which is the
    opposite of what the workspace is for.

    `NOT_APPROVED_CAUSE` is read as a field. It used to be sliced out of the
    full sentence by splitting on an em-dash and a full stop, which coupled
    this renderer to that sentence's punctuation — adding either character to
    the refusal would have quietly truncated what the model is told.

    Quoted rather than paraphrased because the model matches on what it
    actually receives, and the "not approved" wording is load-bearing in a way
    a paraphrase loses: it covers a decline AND an expired prompt, and the
    runtime says so on purpose — telling the operator they declined something
    they never saw is a bug that reached them once already.
    """
    from tesseract.permissions.decide import DENIED_PREFIX, NOT_APPROVED_CAUSE

    cause = NOT_APPROVED_CAUSE
    return (
        f"- **Not approved** — {cause}.\n"
        f"- **`{DENIED_PREFIX}`** — the refusal text begins with those two words."
    )


def render_bash_classes() -> str:
    """What the bash gate refuses, and what it merely asks about.

    IS-5 closed this drift by writing the section to state no list at all,
    which is safe and leaves the model told less than it could be — it cannot
    warn the operator a prompt is coming, and it offers to retry things no
    answer will ever let through.

    **Classes, never patterns.** A check that prints its own regex is an
    attack hint in an audit log, and the module says so where it defines them.
    `test_no_pattern_reaches_the_document` holds this to it.

    Grouped by posture rather than listed check by check: this rides every
    turn, and one bullet per check reads as the document's subject rather than
    as a footnote to the one paragraph it belongs to.
    """
    from tesseract.permissions.bash_security import rules

    rows = rules()
    # `mixed` sits under ASK and nowhere else. Its own sentence carries the
    # half that is refused outright, so the reader is not told less than the
    # truth — and repeating the row in both lists made each list say something
    # the other contradicted.
    asks = [str(r["refuses"]) for r in rows if r["posture"] in ("ask", "mixed")]
    blocked = [str(r["refuses"]) for r in rows if r["posture"] == "blocked"]
    # Bulleted, not run together with a separator: several descriptions carry
    # a dash or a semicolon of their own, so any inline separator lands in the
    # middle of the punctuation already there and the list stops being
    # scannable — for the model as much as for the operator reading the file.
    def _list(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    return (
        "**These reach the operator as a prompt.** Say what you are about to "
        f"run before you run it.\n\n{_list(asks)}\n\n"
        "**These are refused outright**, in every security mode, and no "
        f"approval relaxes one — never offer to retry.\n\n{_list(blocked)}"
    )


def render_temporal_fields() -> str:
    """Which fields the `Right now` block carries."""
    from tesseract.brain.prompt import TEMPORAL_FIELDS

    listed = ", ".join(f"`{field}`" for field in TEMPORAL_FIELDS[:-1])
    return (
        f"The `Right now` block at the end of this prompt carries {listed} and "
        f"`{TEMPORAL_FIELDS[-1]}`."
    )


def render_channel_attachment_statuses() -> str:
    """What an inbound attachment block can say, and what each one means.

    Both halves from `_channel_attachment.py`: the status set is its Literal
    and the meanings are the mapping beside it. A status added there with no
    meaning fails here rather than reaching the model unexplained.
    """
    from tesseract.integrations._channel_attachment import (
        _STATUS_ALLOWED,
        STATUS_MEANING,
    )

    unexplained = sorted(_STATUS_ALLOWED - set(STATUS_MEANING) - {"ready"})
    if unexplained:
        raise KeyError(
            f"channel attachment status {unexplained} has no line in "
            "STATUS_MEANING. A status the runtime can emit and the prompt "
            "cannot explain is one the model meets with nothing to go on."
        )
    rows = [
        f'- `<channel_attachment status="{status}">` — {STATUS_MEANING[status]}.'
        for status in sorted(STATUS_MEANING)
    ]
    return "\n".join(rows)


def render_channel_decoded_kinds() -> str:
    """What arrives readable, and what arrives as a file only.

    The authored paragraph this replaces offered "I can't transcribe voice
    yet" as the model's example apology. Voice has been transcribed since CR-2
    — `_decode_voice` — and so have photos and documents. An apology for a
    capability the runtime has is worse than no example, because the model
    reads it as the answer rather than as a template.
    """
    from tesseract.integrations.telegram.bridge import DECODED_KINDS, PERSISTED_KINDS

    read = ", ".join(f"`{kind}` ({how})" for kind, how in sorted(DECODED_KINDS.items()))
    stored = ", ".join(f"`{kind}`" for kind in sorted(PERSISTED_KINDS))
    return (
        f"Read for you before the turn starts: {read}. Those arrive as text "
        "you can act on.@N@@N@"
        f"Fetched and stored but never read: {stored}. You can refer to one in "
        "a later turn by what it was, but you have not seen inside it."
    ).replace("@N@", chr(10))


OPERATING_INSERTS: dict[str, Callable[[], str]] = {
    "state-read-prefixes": render_state_read_prefixes,
    "memory-probe": render_memory_probe,
    "gate-outcomes": render_gate_outcomes,
    "bash-classes": render_bash_classes,
    "temporal-fields": render_temporal_fields,
}

#: `CHANNEL.md` says what is DIFFERENT about answering through a channel, and
#: nothing else — a channel is the same funnel through another door, so
#: describing how to work on one is teaching the assistant something it
#: already knows. Both regions are inbound: what arrived readable, and what a
#: block says when something did not. The outbound verbs left when it became
#: clear the tool map already carries them on every turn, and the gate
#: paragraph left when the document started saying the gate is unchanged.
CHANNEL_INSERTS: dict[str, Callable[[], str]] = {
    "channel-attachment-statuses": render_channel_attachment_statuses,
    "channel-decoded-kinds": render_channel_decoded_kinds,
}

#: Every renderer, for the "no renderer produces this region" check. Which
#: subset a given document must carry is `_targets`' business — a document
#: holding a region for another document's renderer is a mistake, and so is
#: one missing a region for its own.
INSERTS: dict[str, Callable[[], str]] = {**OPERATING_INSERTS, **CHANNEL_INSERTS}


class Target(NamedTuple):
    """One document and the renderers it is required to carry."""

    path: Path
    inserts: dict[str, Callable[[], str]]


def _targets() -> list[Target]:
    """Each document, in both trees, with the renderers that belong to it.

    Both trees, always. Rendering only the live copy leaves the seed stale and
    every fresh install starts wrong; rendering only the seed leaves this
    machine's assistant reading the old text on every turn.

    The renderer set is per document rather than global. `OPERATING.md` must
    carry all five of its regions and none of the channel's, and vice versa —
    a global set would make every document's completeness check pass as long
    as SOME document held the region.
    """
    found = [
        Target(TESSERACT_DIR / "workspace" / "OPERATING.md", OPERATING_INSERTS),
        Target(TESSERACT_DIR / "workspace" / "_shipping" / "OPERATING.md", OPERATING_INSERTS),
        Target(TESSERACT_DIR / "workspace" / "CHANNEL.md", CHANNEL_INSERTS),
        Target(TESSERACT_DIR / "workspace" / "_shipping" / "CHANNEL.md", CHANNEL_INSERTS),
    ]
    missing = [t.path for t in found if not t.path.is_file()]
    if missing:
        raise FileNotFoundError(
            f"{', '.join(str(p) for p in missing)} is missing. This script names "
            "its documents explicitly; a renamed one must be renamed here in the "
            "same pass, or generation passes by having nothing left to write."
        )
    return found


def apply(text: str, *, path: Path, inserts: dict[str, Callable[[], str]] | None = None) -> str:
    """Every marked region replaced by what the code says it should hold."""
    inserts = INSERTS if inserts is None else inserts
    seen: set[str] = set()
    newline = "\r\n" if "\r\n" in text else "\n"

    # Counted before anything is matched. A region body that itself contains a
    # close marker makes the lazy match stop early, and the real close plus
    # everything after it is left un-substituted — a silently mangled document,
    # written by a script whose output is inlined on every turn. Counting turns
    # that into a refusal.
    opens, closes = text.count(_OPEN_PREFIX), text.count(_CLOSE)
    if opens != closes:
        raise ValueError(
            f"{path.name} has {opens} generated-region opener(s) and {closes} "
            f"closer(s). A region is unclosed, or a body contains the literal "
            f"{_CLOSE!r} — either way the boundaries are ambiguous, and "
            "rewriting from ambiguous boundaries corrupts the document."
        )

    def _sub(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in INSERTS:
            raise KeyError(
                f"{path.name} carries a generated region named {name!r} that no "
                f"renderer produces. Known: {sorted(INSERTS)}."
            )
        seen.add(name)
        body = INSERTS[name]().replace("\n", newline)
        return f"{match.group('open')}{body}{match.group('close')}"

    out = _MARKER.sub(_sub, text)
    unplaced = sorted(set(inserts) - seen)
    if unplaced:
        raise KeyError(
            f"{path.name} has no region for {unplaced}. A renderer with nowhere "
            "to land is a fact the document has silently stopped stating."
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if a generated region is stale")
    parser.add_argument("--write", action="store_true",
                        help="rewrite the generated regions in place")
    args = parser.parse_args(argv)
    if not (args.check or args.write):
        parser.error("pass --check or --write")

    stale: list[str] = []
    for target in _targets():
        path = target.path
        have = _read(path)
        want = apply(have, path=path, inserts=target.inserts)
        rel = path.relative_to(ROOT).as_posix()
        if have == want:
            print(f"[ok] {rel}")
            continue
        if args.write:
            _write(path, want)
            print(f"  wrote {rel}")
        else:
            stale.append(rel)

    if stale:
        print(
            "\nstale generated regions in: " + ", ".join(stale)
            + "\nrun: python -m tesseract.scripts.generate_workspace --write",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
