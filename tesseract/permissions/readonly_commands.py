"""G-1 read-only self-verification allowlist (lean-agent-os Phase 1, Task 5).

Grants the `bash` tool an AUTO carve-out for a small, config-defined set of
read-only invocations (pytest, read-only git, the boot-smoke health probe)
so TARS can self-verify without an operator ASK prompt in every security
mode — not only `headless`.

This module is consulted from `PermissionPolicy.resolve_posture` only.
`bash_security.py`'s 25 numbered checks (19 absolute-DENY + 6 forced-ASK)
run first in `decide.evaluate`, before policy is ever reached — a command
that matches this allowlist but also trips a security check (e.g. `git
show; rm -rf /` hitting check 24) never gets here.

Matching is intentionally conservative: exact-prefix against the
configured list, rejecting any command that carries a byte capable of
smuggling a write past a read-only-looking prefix — redirection (`<`,
`>`), piping/backgrounding (`|`, single `&`), chaining (`;`, newline/CR),
substitution (backtick, bare `$`, parens), or an escaping backslash. This
is a byte-set rejection rather than a substring denylist so it also catches
single `&` (background/OR), `<` (input redirection), bare `$` (all
expansion forms, not just `$(`), and newline-based command smuggling.

A second, independent check rejects any argument token that is itself a
write-producing flag on one of the allowlisted programs — `curl -o`/`-O`/
`--output`, `git --output=`, `pytest --junitxml`/`--html`/`--json-report`/
`--cov-report`/`--basetemp`/etc. These need no shell metacharacter at all
(`curl http://127.0.0.1:8000/api/health -o tesseract/kernel/tools/bash_tool.py`
is a single argv, no piping or redirection) so the byte-set check alone
does not see them; without this second gate a "read-only-looking" allowlist
match could silently overwrite an arbitrary path with zero operator ASK.

Two allowlist shapes:

- ``allowlist`` — prefix entries. A command matches if it equals the
  entry, or starts with ``entry + " "``. This is where flag-bearing
  invocations live (git status/log/diff/show, pytest scoped under a
  trailing-``/`` path). The `_WRITE_FLAG_PREFIXES` token check still
  applies to every prefix match. An entry ending in ``/`` is a
  *path-scoped* prefix: the boundary is the slash itself (no extra
  space required before the first path segment), and any remainder
  containing ``..`` is rejected — this closes a `pytest
  tesseract/tests/../../evil.py` traversal without touching commands
  matched by non-path prefixes (`git log HEAD..main` must keep working).
- ``exact_allowlist`` — whole-string entries only, no trailing
  arguments accepted at all. Chosen over "entry objects with an
  `exact: true` flag" because it keeps the yaml format a flat list of
  strings in both cases (`bash_readonly_allowlist` /
  `bash_readonly_exact_allowlist`) — no per-entry schema to parse — and
  makes the security property visible from the key name alone: nothing
  in `bash_readonly_exact_allowlist` will ever prefix-match. This is
  where the curl health probe lives: `curl ... http://127.0.0.1:8000/api/health`
  needs no arguments to do its job, so any variant carrying trailing
  flags (`-o`, `-O`, or a bundled short-flag cluster like `-sO`/`-so<path>`
  that `_WRITE_FLAG_PREFIXES`'s token-prefix check cannot see through)
  is refused outright rather than laundered through a prefix match.
"""

from __future__ import annotations

_DANGEROUS_CHARS: frozenset[str] = frozenset(
    {"\n", "\r", "&", "|", ";", "<", ">", "`", "$", "(", ")", "\\"}
)

# Argv tokens (exact or prefix) that write output to a caller-controlled
# path on one of the allowlisted read-only programs. Checked per
# whitespace-split token so `--output=foo` and `-ofoo` (no space) are
# caught alongside `--output foo` / `-o foo`.
_WRITE_FLAG_PREFIXES: tuple[str, ...] = (
    "-o",             # curl -o/-O<file>, pytest -o key=value (ini override)
    "-O",             # curl -O (remote-name)
    "--output",       # curl --output[=FILE], git diff/show --output=<file>
    "--junit",        # pytest --junitxml / --junit-xml
    "--result-log",   # pytest legacy report flag
    "--report-log",   # pytest report-log
    "--resultlog",    # pytest legacy report flag (no dash)
    "--html",         # pytest-html
    "--json-report",  # pytest-json-report
    "--cov-report",   # pytest-cov file report
    "--basetemp",     # pytest — writes under an arbitrary base temp dir
)


def is_readonly_allowed(
    command: str,
    allowlist: list[str],
    exact_allowlist: list[str] | None = None,
) -> bool:
    """True if `command` is a safe match against `allowlist` (prefix) or
    `exact_allowlist` (whole-string only).

    Prefix matching requires the command (after stripping surrounding
    whitespace) to equal an entry, start with `entry + " "`, or — for an
    entry ending in `/` — simply start with the entry (the slash is
    itself the boundary). Path-scoped (`/`-suffixed) entries additionally
    reject a match whose remainder contains `..` (traversal); this check
    is scoped to those entries only — `git log HEAD..main` still matches
    the plain `git log` prefix.

    Exact matching requires the full stripped command to equal one of
    `exact_allowlist` verbatim — no trailing arguments of any kind, so a
    bundled short-flag cluster (`-sO`, `-so<path>`) can never ride along.

    Any character in `_DANGEROUS_CHARS` disqualifies the command outright
    (checked before either allowlist). Any argv token starting with a
    `_WRITE_FLAG_PREFIXES` entry disqualifies a *prefix* match — exact
    matches skip this check because an exact match by definition carries
    no extra tokens.
    """
    stripped = command.strip()
    if not stripped:
        return False
    if any(ch in _DANGEROUS_CHARS for ch in stripped):
        return False
    if stripped in (exact_allowlist or ()):
        return True
    if any(token.startswith(_WRITE_FLAG_PREFIXES) for token in stripped.split()):
        return False
    for prefix in allowlist:
        if prefix.endswith("/"):
            if stripped.startswith(prefix) and ".." not in stripped[len(prefix):]:
                return True
            continue
        if stripped == prefix or stripped.startswith(prefix + " "):
            return True
    return False
