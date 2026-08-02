"""Bash command security — 26 numbered checks (20 absolute DENY + 6 forced-ASK).

Checks are numbered, not named — prevents attack hints in logs. Each
check returns (check_number, posture) on failure, None on pass. The
``posture`` string is one of:

  - ``"blocked"`` — absolute DENY. Audit-evasion + kernel/host attacks
    that have no benign agent use; sandboxing doesn't change their risk.
    Cannot be relaxed by hooks, plugins, skills, or agents.
  - ``"ask"`` — forced ASK posture. Tier-shifted 2026-05-08 per
    ``Docs/Doclog/2026-05-03.md`` so MO-8 provisional-candidate KPI
    runs and MO-9 ``crontab`` self-scheduling can run without blanket
    denial. Check 10 joined 2026-07-12 (``Docs/Doclog/2026-07-12.md``) —
    its pattern false-positives on quoted regex literals. Requires an
    operator-attended approval channel; cannot auto-allow. Hits checks
    8, 10, 15, 17, 18, 24.

Evaluation order is the order of ``_CHECKS``, not the numbering: check 26
(sealed-tree writes) runs ahead of check 24 so ``rm -rf app/`` is denied
rather than offered as an ASK.

Call sites must branch on the second tuple element. The 20 DENY checks
remain the canonical hard floor; the 6 ASK checks are surfaced through
``decide.evaluate``'s ``ask_fn`` flow when an operator is attached, and
hard-fail (mission BLOCKED) when no approval channel is wired.
"""

from __future__ import annotations

import re
import unicodedata


def check(command: str) -> tuple[int, str] | None:
    """Run all 26 security checks. Returns (check_num, posture) on failure.

    ``posture`` is ``"blocked"`` for the 20 absolute DENY checks
    (1-7, 9, 11-14, 16, 19-23, 25, 26) and ``"ask"`` for the 6 forced-ASK
    checks (8, 10, 15, 17, 18, 24). Returns ``None`` when every check
    passes.
    """
    for checker in _CHECKS:
        result = checker(command)
        if result is not None:
            return result
    return None


def _check_01(cmd: str) -> tuple[int, str] | None:
    """Null bytes in command."""
    if "\x00" in cmd:
        return 1, "blocked"
    return None


def _check_02(cmd: str) -> tuple[int, str] | None:
    """Unicode whitespace (non-ASCII spaces that bypass tokenization)."""
    for char in cmd:
        if unicodedata.category(char) in ("Zs", "Zl", "Zp") and char != " ":
            return 2, "blocked"
    return None


def _check_03(cmd: str) -> tuple[int, str] | None:
    """IFS injection — setting IFS to override command parsing."""
    if re.search(r"\bIFS\s*=", cmd):
        return 3, "blocked"
    return None


def _check_04(cmd: str) -> tuple[int, str] | None:
    """Zsh zmodload — loads arbitrary kernel modules."""
    if re.search(r"\bzmodload\b", cmd):
        return 4, "blocked"
    return None


def _check_05(cmd: str) -> tuple[int, str] | None:
    """Zsh sysopen — direct file descriptor manipulation."""
    if re.search(r"\bsysopen\b", cmd):
        return 5, "blocked"
    return None


def _check_06(cmd: str) -> tuple[int, str] | None:
    """Zsh ztcp — raw TCP from shell."""
    if re.search(r"\bztcp\b", cmd):
        return 6, "blocked"
    return None


def _check_07(cmd: str) -> tuple[int, str] | None:
    """Zsh equals expansion (=curl → /path/to/curl, bypasses deny rules)."""
    if re.search(r"(?:^|\s)=[a-zA-Z]", cmd):
        return 7, "blocked"
    return None


def _check_08(cmd: str) -> tuple[int, str] | None:
    """eval / source / . execution — ASK (operator approval required).

    Tier-shifted 2026-05-08: ``eval``/``source``/``. script`` are
    legitimate when MO-8 candidate tools or operator workflows need
    them, but always operator-attended. The ``printf '\\xNN' | sh``
    decode-to-exec pattern below is still ``"blocked"`` — there is
    no benign use.
    """
    if re.search(r"(?:^|\s|;|&&|\|\|)\s*(?:eval|source)\s", cmd):
        return 8, "ask"
    # Dot-source: `. script` but not `./script`
    if re.search(r"(?:^|\s|;|&&|\|\|)\s*\.\s+\S", cmd):
        return 8, "ask"
    # printf decode-to-exec: printf '\xNN' | sh
    if re.search(r"\bprintf\b.*\\x[0-9a-fA-F].*\|\s*(bash|sh|zsh|python|perl)", cmd):
        return 8, "blocked"
    return None


def _check_09(cmd: str) -> tuple[int, str] | None:
    """Backtick command substitution (prefer $() which is auditable)."""
    if "`" in cmd:
        return 9, "blocked"
    return None


def _check_10(cmd: str) -> tuple[int, str] | None:
    """Process substitution that hides commands — ASK.

    Tier-shifted 2026-07-12: the pattern matches ``>(`` / ``<(``
    anywhere, including inside quoted strings — a regex literal like
    ``'<body[^>]*>(.*)'`` in a python one-liner is a guaranteed benign
    hit (live incident, session 2026-07-12-1818). Real process
    substitution has legitimate uses too (``diff <(a) <(b)``); the full
    command is in the approval ledger either way. Operator-attended
    only — never auto-allow.
    """
    if re.search(r"[<>]\(", cmd):
        return 10, "ask"
    return None


def _check_11(cmd: str) -> tuple[int, str] | None:
    """Fork bomb patterns."""
    if re.search(r":\(\)\s*\{.*\}.*;\s*:", cmd):
        return 11, "blocked"
    if re.search(r"\bfork\s*\(\)", cmd):
        return 11, "blocked"
    return None


# Contexts that actually decode \xNN / \NNN escapes into bytes: printf,
# echo -e (any flag cluster containing `e`), and ANSI-C $'...' quoting.
# Outside these, a backslash escape is inert text — which matters on
# Windows, where `tars-workshop\2026-07-12` matches the octal pattern
# (live incident, session 2026-07-12-1818) and `build\x64` the hex one.
_ESCAPE_DECODER_RE = re.compile(r"\bprintf\b|\becho\s+-\w*e")

# Escapes + a pipe into a shell/interpreter is a decode-and-exec chain
# no matter what produced the bytes (python print, perl, awk, ...) —
# review finding 2026-07-12: gating on printf/echo alone let
# `python -c "print('\x63...')" | sh` through.
_PIPE_TO_INTERPRETER_RE = re.compile(
    r"\|\s*(bash|sh|zsh|python[23]?|perl|ruby|pwsh|powershell)\b"
)


def _check_12(cmd: str) -> tuple[int, str] | None:
    """Hex/octal escape sequences that encode malicious commands.

    Precision-fixed 2026-07-12: bare ``\\xNN`` / ``\\NNN`` only fires
    alongside a decoding context — ``printf`` / ``echo -e``, or any
    pipe into a shell/interpreter (decode-and-exec chain). Backslash
    path segments with neither are inert. ``$'\\...'`` decodes by
    itself and stays an unconditional hit.
    """
    if re.search(r"\$'\\", cmd):
        return 12, "blocked"
    has_escape = re.search(r"\\x[0-9a-fA-F]{2}", cmd) or re.search(r"\\[0-7]{3}", cmd)
    if has_escape and (_ESCAPE_DECODER_RE.search(cmd) or _PIPE_TO_INTERPRETER_RE.search(cmd)):
        return 12, "blocked"
    return None


def _check_13(cmd: str) -> tuple[int, str] | None:
    """Base64 decode piped to execution."""
    if re.search(r"base64\s+(-d|--decode)", cmd) and re.search(r"\|\s*(bash|sh|zsh|python|perl|ruby)", cmd):
        return 13, "blocked"
    return None


def _check_14(cmd: str) -> tuple[int, str] | None:
    """dd to raw devices."""
    if re.search(r"\bdd\b.*\bof\s*=\s*/dev/", cmd):
        return 14, "blocked"
    return None


def _check_15(cmd: str) -> tuple[int, str] | None:
    """Curl/wget piped to shell execution — ASK (operator approval required).

    Tier-shifted 2026-05-08: install scripts (``curl https://...| sh``)
    are an MO-8 KPI-suite necessity for candidate tools that need
    third-party dependencies. Operator-attended only — no auto-allow.
    A future URL-allowlist policy hook can upgrade specific domains to
    AUTO via ``permissions.yaml``; this gate stays ASK at the security
    layer.
    """
    if re.search(r"(curl|wget)\s.*\|\s*(bash|sh|zsh|python|perl)", cmd):
        return 15, "ask"
    return None


def _check_16(cmd: str) -> tuple[int, str] | None:
    """Network reverse shell patterns."""
    if re.search(r"\b(nc|ncat|netcat)\s.*-[elp]", cmd):
        return 16, "blocked"
    if re.search(r"/dev/tcp/", cmd):
        return 16, "blocked"
    if re.search(r"bash\s+-i\s+>&\s*/dev/tcp", cmd):
        return 16, "blocked"
    return None


def _check_17(cmd: str) -> tuple[int, str] | None:
    """Python/perl/ruby one-liners with os/system/exec calls — ASK.

    Tier-shifted 2026-05-08: ad-hoc ``python -c "import os; ..."`` is a
    legitimate operator workflow that the original blanket DENY made
    needlessly painful. Still operator-attended.
    """
    if re.search(r"python[23]?\s+-c\s+.*(?:import\s+os|os\.system|subprocess|exec\()", cmd):
        return 17, "ask"
    if re.search(r"perl\s+-e\s+.*(?:system|exec)", cmd):
        return 17, "ask"
    if re.search(r"ruby\s+-e\s+.*(?:system|exec|`)", cmd):
        return 17, "ask"
    return None


def _check_18(cmd: str) -> tuple[int, str] | None:
    """Crontab modification — ASK (operator approval required).

    Tier-shifted 2026-05-08: MO-9 Autonomous Loop needs ``crontab`` for
    persistent local cron registration. Operator-attended every time;
    audit-ledger row records the change for later inspection.
    """
    if re.search(r"\bcrontab\b", cmd):
        return 18, "ask"
    return None


def _check_19(cmd: str) -> tuple[int, str] | None:
    """Privilege escalation."""
    if re.search(r"\b(sudo|su\s+-|doas)\b", cmd):
        return 19, "blocked"
    if re.search(r"\bchmod\s+[0-7]*7[0-7]*\b", cmd):
        return 19, "blocked"
    if re.search(r"\bchmod\s+[ugo]*\+s", cmd):
        return 19, "blocked"
    return None


def _check_20(cmd: str) -> tuple[int, str] | None:
    """Environment variable manipulation that affects child processes."""
    if re.search(r"\bexport\s+(?:PATH|LD_PRELOAD|LD_LIBRARY_PATH|DYLD_)", cmd):
        return 20, "blocked"
    if re.search(r"\bLD_PRELOAD\s*=", cmd):
        return 20, "blocked"
    return None


def _check_21(cmd: str) -> tuple[int, str] | None:
    """Disk/filesystem operations."""
    if re.search(r"\b(mkfs|fdisk|parted|mount|umount)\b", cmd):
        return 21, "blocked"
    return None


def _check_22(cmd: str) -> tuple[int, str] | None:
    """Service/systemd manipulation."""
    if re.search(r"\b(systemctl|service)\s+(start|stop|restart|enable|disable)\b", cmd):
        return 22, "blocked"
    return None


def _check_23(cmd: str) -> tuple[int, str] | None:
    """HackerOne eval bypass — malformed token injection via variable names."""
    # Pattern: ${var_with_special_chars} where the variable expansion
    # could inject commands after shell expansion
    if re.search(r"\$\{[^}]*[;&|`]\s*[^}]*\}", cmd):
        return 23, "blocked"
    return None


def _check_24(cmd: str) -> tuple[int, str] | None:
    """Common destructive verbs — rm -rf, del /s, git push --force — ASK.

    Tier-shifted 2026-05-08: legitimate cleanup commands hit this gate
    constantly (``rm -rf .pytest-tmp``, workspace teardown). Operator
    confirmation is the right floor — workspace/path-fit guards belong
    in a higher-level policy layer (MO-8 KPI-runner sandbox path), not
    here at the universal security check.
    """
    # rm with both -r (or --recursive) and -f (or --force), flags in any order.
    if re.search(r"\brm\s+(?=.*(?:-\w*r|--recursive))(?=.*(?:-\w*f|--force))", cmd):
        return 24, "ask"
    # Windows recursive delete.
    if re.search(r"\bdel\s+/[sS]\b", cmd):
        return 24, "ask"
    # Windows recursive rmdir with /s.
    if re.search(r"\b(?:rd|rmdir)\s+/[sS]\b", cmd, re.IGNORECASE):
        return 24, "ask"
    # git force-push (permits --force-with-lease, which is safe).
    if re.search(r"\bgit\s+push\b.*--force(?!-with-lease)", cmd):
        return 24, "ask"
    if re.search(r"\bgit\s+push\b.*\s-f(?:\s|$)", cmd):
        return 24, "ask"
    return None


# Both spellings on purpose. The repo-relative form is what a dev checkout
# produces; the bare form is what a real install produces, where config lives
# at `<home>/config/` and the install directory is `com.tesseract.mirror` — no
# `tesseract/config/` substring appears anywhere in that path.
_LOCKED_POSTURE_YAMLS: tuple[str, ...] = (
    "tesseract/config/permissions.yaml",
    "tesseract/config/roles.yaml",
    "tesseract/config/providers.yaml",
    "tesseract/config/mirror.yaml",
    "tesseract/config/mcp.yaml",
    "config/permissions.yaml",
    "config/roles.yaml",
    "config/providers.yaml",
    "config/mirror.yaml",
    # `mcp.yaml` decides what an MCP client may do. Locking it in the tool
    # layer alone would leave the shell as an open door to the same file.
    "config/mcp.yaml",
)

# Redirect / write verbs checked in an 80-char prefix before the locked path.
# `>` and `>>` use a tight regex (only optional whitespace allowed between
# verb and path) to avoid false-positives on comparison operators elsewhere.
_REDIRECT_VERBS_WORD: tuple[str, ...] = (
    "tee ", "tee.exe ", "tee\t", "tee.exe\t",
    "sed -i", "sed.exe -i",
    "set-content", "out-file", "add-content",
    "writelines", "write_text", "write_bytes",
    "cp ", "copy ", "copy.exe ", "move ", "mv ",
)

# `>` and `>>` must be the last non-whitespace characters in the before-window
# — i.e. the token immediately before the locked path (with optional spaces).
_REDIRECT_RE = re.compile(r">{1,2}\s*$")


def _check_25(cmd: str) -> tuple[int, str] | None:
    """Absolute DENY: write to permissions/roles/providers/mirror.yaml.

    Belt-and-braces over the SU-1 file_write lockdown. Closes the bash-bypass
    class (echo X >> config.yaml, sed -i, Set-Content, etc.). Read access is
    unaffected.

    For `>` / `>>`: the verb must be the last token before the locked path
    (only whitespace in between). This rules out `>` used as a comparison
    operator or inside a string elsewhere in the command.
    For word verbs (tee, sed -i, cp, etc.): checked in an 80-char prefix.
    `open(` is checked in the same prefix; the accepted over-trigger (read
    calls also match) is per spec §2.1.
    """
    lower = cmd.lower()
    for yaml_path in _LOCKED_POSTURE_YAMLS:
        for variant in (yaml_path, yaml_path.replace("/", "\\")):
            idx = lower.find(variant)
            if idx < 0:
                continue
            before = lower[max(0, idx - 80):idx]
            # Tight check: `>` / `>>` must immediately precede the path.
            if _REDIRECT_RE.search(before):
                return 25, "blocked"
            # Word verbs: substring match in the 80-char prefix.
            if any(verb in before for verb in _REDIRECT_VERBS_WORD):
                return 25, "blocked"
            # Spec-accepted defensive over-trigger: open( on read also matches.
            # Operators should use the file_read tool for read access.
            if "open(" in before:
                return 25, "blocked"
    return None


# Sealed trees, as a path token opens a segment: `app/…`, `./app/…`,
# `runtime/…`. Deliberately NOT matched when nested (`tars-workshop/app/x`) —
# that is the operator's own directory that happens to share a name, and the
# seal is about the install's `app/`, not about the word.
_SEALED_SEGMENT_RE = re.compile(r"""(?:^|[\s"'=(;|&])(?:\./)?(app|runtime)/""")

# Write verbs for the seal check. Broader than `_REDIRECT_VERBS_WORD` because
# the target here is a whole tree rather than four known files: creating,
# truncating and permission-changing all count as edits to a sealed tree.
_SEAL_WRITE_VERBS: tuple[str, ...] = (
    "tee ", "tee.exe ",
    "sed -i", "sed.exe -i",
    "set-content", "out-file", "add-content",
    "writelines", "write_text", "write_bytes",
    "cp ", "copy ", "copy.exe ", "move ", "mv ",
    "rm ", "rm.exe ", "rmdir ", "del ", "erase ", "remove-item",
    "touch ", "mkdir ", "md ", "truncate ", "chmod ", "chown ", "attrib ",
    "install ", "patch ", "unzip ", "tar ",
)

# The subset that takes source and destination, where only the destination is
# a write. Copying a file OUT of the sealed tree is a read.
_SEAL_COPY_VERBS: tuple[str, ...] = (
    "cp ", "copy ", "copy.exe ", "move ", "mv ", "install ",
)


def _check_26(cmd: str) -> tuple[int, str] | None:
    """Absolute DENY: write verbs targeting the sealed `app/` or `runtime/`.

    The write boundary in `path_validator` covers the file tools, but `bash`
    never reaches it, and neither do the CLIs an agent can launch through it.
    Reads are deliberately untouched — `app/` is read-only, which means
    readable: TARS can inspect his own source, just not edit it.

    Path-shaped and therefore approximate by construction. It catches the two
    forms an agent actually produces — a relative path from the install root
    and a resolved absolute one. Known and accepted misses, in rough order of
    how likely they are to come up:

    * `cd app && <relative write>` — once the shell has moved, the command
      text carries no `app/` at all. Blocking `cd app` outright would break
      ordinary read-only exploration, which is explicitly allowed.
    * paths reached through `..`, assembled from shell variables, or split
      across quoting.
    * the reverse: an unrelated external project with its own top-level
      `app/` (a Next.js or Flask layout) is denied identically, because the
      check sees command text and has no cwd to disambiguate with.

    This is a guard against the accident, not a sandbox against a determined
    process; `SEALED.md` and the generated `CLAUDE.md`/`AGENTS.md` at the tree
    root state the rule for the cases it misses.
    """
    lower = cmd.lower()

    def _blocked_at(idx: int) -> bool:
        before = lower[max(0, idx - 80):idx]
        if _REDIRECT_RE.search(before):
            return True
        if any(verb in before for verb in _SEAL_WRITE_VERBS):
            # Two-operand verbs write to their LAST operand. `cp app/x home/y`
            # reads out of the seal, which is allowed — only a sealed path in
            # the destination slot is a write. One remaining operand means the
            # match is that destination.
            if any(verb in before for verb in _SEAL_COPY_VERBS):
                return len(lower[idx:].split()) == 1
            return True
        return "open(" in before and "'r'" not in before and '"r"' not in before

    for match in _SEALED_SEGMENT_RE.finditer(lower):
        if _blocked_at(match.start(1)):
            return 26, "blocked"

    # Resolved absolute paths: the tree names carry no meaning on their own
    # once a path is absolute, so compare against the real roots.
    try:
        from tesseract.paths import app_dir, runtime_dir

        roots = (str(app_dir()), str(runtime_dir()))
    except Exception:  # noqa: BLE001 — the segment matcher above still stands
        return None
    for root in roots:
        for variant in (root.lower(), root.lower().replace("\\", "/")):
            idx = lower.find(variant)
            if idx >= 0 and _blocked_at(idx):
                return 26, "blocked"
    return None


# Order is evaluation order; the numbers are labels, not positions. `_check_26`
# runs ahead of `_check_24` deliberately: 24 forces ASK on recursive-destructive
# verbs, so `rm -rf app/` would otherwise be one operator `y` away from deleting
# the sealed tree. A DENY on the sealed trees outranks an ASK on the verb.
_CHECKS: list = [
    _check_01, _check_02, _check_03, _check_04, _check_05,
    _check_06, _check_07, _check_08, _check_09, _check_10,
    _check_11, _check_12, _check_13, _check_14, _check_15,
    _check_16, _check_17, _check_18, _check_19, _check_20,
    _check_21, _check_22, _check_23, _check_26, _check_24,
    _check_25,
]
