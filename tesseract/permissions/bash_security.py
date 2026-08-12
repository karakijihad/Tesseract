"""Bash command security — 26 numbered checks (20 absolute DENY + 6 forced-ASK).

Checks are numbered, not named — prevents attack hints in logs. Each
check returns (check_number, posture) on failure, None on pass. The
``posture`` string is one of:

  - ``"blocked"`` — absolute DENY. Audit-evasion + kernel/host attacks
    that have no benign agent use; sandboxing doesn't change their risk.
    Cannot be relaxed by hooks, plugins, skills, or agents.
  - ``"ask"`` — forced ASK posture. Tier-shifted 2026-05-08 per
    so provisional-candidate KPI
    runs and MO-9 ``crontab`` self-scheduling can run without blanket
    denial. Check 10 joined later —
    its pattern false-positives on quoted regex literals. Requires an
    operator-attended approval channel; cannot auto-allow. Hits checks
    8, 10, 15, 17, 18, 24.

Posture, not position, decides which result is returned: any ``blocked``
beats any ``ask``, whatever order the two checkers sit in. Order within
``_CHECKS`` still decides which of two same-posture checks is named — check
26 (sealed-tree writes) runs ahead of check 24 so ``rm -rf app/`` is
reported as the seal violation it is.

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

    A ``blocked`` result wins over an ``ask`` from any checker, wherever each
    sits in ``_CHECKS``. Returning the first match instead let an ASK trigger
    anywhere in the command hide a DENY later in the list: ``echo crontab &&
    echo x > config/permissions.yaml`` answered check 18's ASK and never
    reached check 25, so an operator ``y`` — the one thing an absolute DENY is
    defined as not accepting — ran the write. The first ASK is still what gets
    reported when nothing is blocked, so approval flows are unchanged.
    """
    first_ask: tuple[int, str] | None = None
    for checker in _CHECKS:
        result = checker(command)
        if result is None:
            continue
        if result[1] == "blocked":
            return result
        if first_ask is None:
            first_ask = result
    return first_ask


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
    hit, observed live. Real process
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
# Windows, where `workshop\2026-07-12` matches the octal pattern
# (observed live) and `build\x64` the hex one.
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
# `runtime/…`. Deliberately NOT matched when nested (`workshop/app/x`) —
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
    "install ", "patch ",
)

# `tar` and `unzip` are not in the list above: their operands do not say where
# they write. Extraction writes to the working directory while naming a source
# archive elsewhere, and listing writes nothing at all — so treating every
# operand as a destination denies `tar -t` and misses `tar -xf /tmp/x.tgz`,
# which lands squarely in the sealed cwd. Mode decides, not operand position.
_TAR_RE = re.compile(r"(?:^|\s)tar(?:\.exe)?\s+(.*)$", re.IGNORECASE)
_UNZIP_RE = re.compile(r"(?:^|\s)unzip(?:\.exe)?\s+(.*)$", re.IGNORECASE)


def _archive_target(segment: str) -> str | None:
    """Where an archive command writes, or None when it only reads.

    Returns ``"."`` for "into the working directory", which the caller resolves
    against the modelled cwd like any other relative target.
    """
    tar = _TAR_RE.search(segment)
    if tar:
        rest = tar.group(1)
        flags = " ".join(t for t in rest.split() if t.startswith("-"))
        if "--list" in flags or re.search(r"-\w*t", flags):
            return None
        if "--extract" in flags or re.search(r"-\w*x", flags):
            into = re.search(r"(?:-C|--directory[=\s])\s*(\S+)", rest)
            return into.group(1) if into else "."
        if "--create" in flags or re.search(r"-\w*c", flags):
            # Creating an archive reads the seal and writes the -f file.
            named = re.search(r"(?:-\w*f|--file[=\s])\s*(\S+)", rest)
            return named.group(1) if named else None
        return None

    unzip = _UNZIP_RE.search(segment)
    if unzip:
        rest = unzip.group(1)
        if re.search(r"(?:^|\s)-[lt]\b", rest):
            return None
        into = re.search(r"-d\s*(\S+)", rest)
        return into.group(1) if into else "."
    return None

# The subset that takes source and destination, where only the destination is
# a write. Copying a file OUT of the sealed tree is a read.
_SEAL_COPY_VERBS: tuple[str, ...] = (
    "cp ", "copy ", "copy.exe ", "move ", "mv ", "install ",
)

# One command string can hold several commands, and a `cd` in an earlier one
# changes where a later one's relative paths land. `|` separates too: the right
# side of a pipe is its own command inheriting the same cwd.
_SEGMENT_SEP_RE = re.compile(r"&&|\|\||[;\n|]")

# `cd <target>`, tolerating cmd.exe's `/d` and wrapping parens from a subshell.
# `pushd` moves the cwd exactly as `cd` does and is the first thing reached for
# when `cd` is refused, so it is the same token here. `Set-Location` (and its
# `sl` alias) is PowerShell's spelling — the shipped platform's default shell.
_CD_RE = re.compile(
    r"^\(*\s*(?:cd|chdir|pushd|set-location|sl)\s+(?:/d\s+|-Path\s+)?(.+?)\s*\)*$",
    re.IGNORECASE,
)

# `popd` returns somewhere only the directory stack knows.
_POPD_RE = re.compile(r"^\(*\s*popd\b", re.IGNORECASE)

# `env -C <dir> <cmd>` / `env --chdir=<dir> <cmd>` — a cwd change with no `cd`.
_ENV_CHDIR_RE = re.compile(
    r"^\(*\s*env\s+(?:-\w+\s+)*(?:-C\s+|--chdir[=\s])\s*(\S+)", re.IGNORECASE
)

# A shell invoked to run a command string carries it in one argument. The outer
# split is quoting-unaware, so without this the nested `cd` is torn from its
# prefix and never recognised. Windows spellings are included because Windows is
# the shipped platform: `cmd /c`, `%ComSpec% /c`, and PowerShell's `-Command`
# reach the same place `bash -c` does.
_NESTED_SHELL_RE = re.compile(
    r"(?:(?:bash|sh|zsh|dash|ksh|pwsh|powershell)(?:\.exe)?\s+(?:-\w+\s+)*"
    r"(?:-c|-Command|-EncodedCommand)"
    r"|(?:cmd(?:\.exe)?|%comspec%)\s+(?:/\w+\s+)*/c)"
    r"\s+(?:\"([^\"]*)\"|'([^']*)')",
    re.IGNORECASE,
)

# Anchored somewhere other than the cwd: POSIX root, UNC/drive-relative, home,
# or a Windows drive. A write to one of these is unaffected by an earlier `cd`.
_ABS_PATH_RE = re.compile(r"^(?:/|\\|~|[a-z]:)", re.IGNORECASE)

# Redirect target. The negative lookbehind keeps fd duplication (`2>&1`) and
# here-strings out; the target is the token that follows.
_REDIRECT_TARGET_RE = re.compile(r"(?<![0-9<>&])>>?\s*([^\s;&|<>()]+)")


def _clean_token(raw: str) -> str:
    return raw.strip().strip('"').strip("'").replace("\\", "/").strip()


def _install_relative(abs_path: str) -> list[str] | None:
    """`abs_path` as components under the install root, or None if outside it.

    Absolute paths carry no `app/` token for the segment matcher, so the seal
    has to be recognised by comparing against the real roots.
    """
    try:
        from tesseract.paths import app_dir, runtime_dir
    except Exception:  # noqa: BLE001 — an unresolvable root is not a seal
        return None

    probe = abs_path.rstrip("/").lower()
    for root_dir, label in ((app_dir(), "app"), (runtime_dir(), "runtime")):
        root = str(root_dir).replace("\\", "/").rstrip("/").lower()
        if probe == root:
            return [label]
        if probe.startswith(root + "/"):
            rest = [p for p in probe[len(root) + 1:].split("/") if p and p != "."]
            return [label, *rest]
    return None


def _walk(position: list[str], parts: list[str]) -> list[str] | None:
    """Apply path components to a position, or None once it climbs out of view.

    `..` popping an empty position means the path went above the install root,
    where this model has nothing left to say.
    """
    walked = list(position)
    for part in parts:
        if part == "..":
            if not walked:
                return None
            walked.pop()
        else:
            walked.append(part)
    return walked


def _resolve(target: str, position: list[str] | None) -> list[str] | None:
    """Where `target` lands, as components under the install root.

    `position` is the modelled cwd (also install-root-relative), or None when
    the cwd is unknown. Returning components rather than a depth is what lets
    `cd ../app` be recognised as re-entering: a counter that bottoms out at the
    first `..` cannot see the rest of the path.
    """
    t = _clean_token(target)
    if not t or t == "-" or t.startswith("~") or t.startswith("$"):
        return None
    if _ABS_PATH_RE.match(t):
        return _install_relative(t)

    parts = [p for p in t.split("/") if p and p != "."]
    if position is None:
        # An unknown cwd only becomes known when the path names a sealed tree
        # outright — anything else could be anywhere.
        if not parts or parts[0].lower() not in ("app", "runtime"):
            return None
        return _walk([parts[0].lower()], parts[1:])
    return _walk(position, parts)


def _is_sealed(position: list[str] | None) -> bool:
    return bool(position) and position[0].lower() in ("app", "runtime")


# `sed -i` may carry an attached backup suffix (`sed -i.bak`), which a plain
# token match misses while the literal-path pass's substring test catches it.
_SED_INPLACE_RE = re.compile(r"(?:^|\s)sed(?:\.exe)?\s+(?:-\w+\s+)*-i\S*(?=\s|$)")

# `dd of=<path>` and `open('<path>', 'w')` write without naming a verb the
# operand scanner would find.
_DD_TARGET_RE = re.compile(r"(?:^|\s)of\s*=\s*([^\s;&|]+)")
_OPEN_TARGET_RE = re.compile(r"open\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]*)['\"]")


def _mask_quotes(segment: str) -> str:
    """`segment` with quoted interiors blanked to a filler, length preserved.

    `git commit -m "fix rm bug"` is not an `rm`. Scanning raw text for verbs
    reads prose inside a quoted argument as a command, and because check 26 is
    a DENY the operator cannot approve past, that false positive is
    unappealable. The filler is non-space so token boundaries survive, and
    equal length keeps every offset usable against the original.
    """
    out = list(segment)
    i, n = 0, len(segment)
    while i < n:
        if segment[i] in ('"', "'"):
            close = segment.find(segment[i], i + 1)
            if close == -1:
                break
            for k in range(i + 1, close):
                out[k] = "Q"
            i = close + 1
        else:
            i += 1
    return "".join(out)


def _operands_from(segment: str, masked: str, start: int) -> list[str]:
    """Tokens after `start`, split on the mask so a quoted path stays one."""
    return [
        segment[start + m.start(): start + m.end()]
        for m in re.finditer(r"\S+", masked[start:])
        if not m.group().startswith("-")
    ]


def _write_targets(segment: str) -> list[str]:
    """Every path this segment might write to.

    Missing a target means missing a denial, so the scan is deliberately
    generous; the caller decides whether a target actually lands in the seal,
    which is where precision belongs. Positions are found in the masked text
    so quoted prose cannot pose as a verb, then read back out of the original
    so a quoted path is still the real path.
    """
    masked = _mask_quotes(segment)
    lower = masked.lower()

    targets: list[str] = [
        segment[m.start(1):m.end(1)] for m in _REDIRECT_TARGET_RE.finditer(masked)
    ]
    targets.extend(segment[m.start(1):m.end(1)] for m in _DD_TARGET_RE.finditer(masked))
    if (archive := _archive_target(masked)) is not None:
        targets.append(archive)
    # `open(` reads its path from inside the quotes the mask just blanked, so
    # it is matched against the original by necessity.
    targets.extend(
        m.group(1) for m in _OPEN_TARGET_RE.finditer(segment) if "r" not in m.group(2)
    )

    for match in _SED_INPLACE_RE.finditer(lower):
        operands = _operands_from(segment, masked, match.end())
        if operands:
            targets.append(operands[-1])

    for verb in _SEAL_WRITE_VERBS:
        token = verb.strip()
        for match in re.finditer(r"(?:^|\s)" + re.escape(token) + r"(?=\s|$)", lower):
            operands = _operands_from(segment, masked, match.end())
            if not operands:
                continue
            # Two-operand verbs write to their last operand only; the rest treat
            # every operand as a target.
            targets.extend([operands[-1]] if verb in _SEAL_COPY_VERBS else operands)

    return targets


def _check_26_after_cd(cmd: str, _depth: int = 0) -> tuple[int, str] | None:
    """The `cd` half of check 26 — a write whose sealed-ness is positional.

    Once the shell has moved into `app/`, the command text carries no `app/`
    for the segment matcher to find, so the write looks like any other. Reads
    stay allowed for the same reason they always were: exploring a sealed tree
    is the point of it being readable, and only a write verb or a redirect
    trips this.

    A target is judged by where it *lands*, not by whether it is spelled
    relatively. A relative target that climbs back out of the sealed tree is
    the operator's business and stays allowed; one that resolves inside it is
    denied, however it was written.
    """
    for inner in _NESTED_SHELL_RE.finditer(cmd):
        # `bash -c "cd app && …"` hides the move from a quoting-unaware split.
        # Bounded recursion — a nested shell inside a nested shell is still a
        # command string, and refusing to look would be the bypass.
        body = inner.group(1) or inner.group(2) or ""
        if _depth < 3 and body and (hit := _check_26_after_cd(body, _depth + 1)):
            return hit

    position: list[str] | None = None
    for segment in _SEGMENT_SEP_RE.split(cmd):
        segment = segment.strip()
        if not segment:
            continue

        if _POPD_RE.match(segment):
            # The directory stack is not modelled; forget where we are rather
            # than assume we stayed.
            position = None
            continue

        cd_match = _CD_RE.match(segment) or _ENV_CHDIR_RE.match(segment)
        if cd_match:
            position = _resolve(cd_match.group(1), position)
            # `env -C dir <cmd>` moves and runs in one segment, so the rest of
            # it still has to be judged.
            if _CD_RE.match(segment):
                continue

        if not _is_sealed(position):
            continue
        for target in _write_targets(segment):
            if _is_sealed(_resolve(target, position)):
                return 26, "blocked"
    return None


def _check_26(cmd: str) -> tuple[int, str] | None:
    """Absolute DENY: write verbs targeting the sealed `app/` or `runtime/`.

    The write boundary in `path_validator` covers the file tools, but `bash`
    never reaches it, and neither do the CLIs an agent can launch through it.
    Reads are deliberately untouched — `app/` is read-only, which means
    readable: the assistant can inspect its own source, just not edit it.

    Path-shaped and therefore approximate by construction. It catches the three
    forms an agent actually produces — a relative path from the install root, a
    resolved absolute one, and a write issued after `cd`-ing into the tree
    (`_check_26_after_cd`, which tracks the cwd across the command's segments so
    reads after the same `cd` stay allowed).

    **The limit is structural, not a backlog.** Reading command text cannot
    enumerate every way a shell can write a file. Two classes are open by
    construction and no addition to the verb list closes them:

    * an interpreter writing through its own API — `python -c "Path('x')
      .write_text(...)"`, `shutil.copy`, `node`, `perl`. The tail is infinite,
      and the alternative (deny every interpreter while the cwd is sealed)
      denies `cd app && python -m pytest`, which is legitimate and common.
    * a path that only exists after expansion — `cd $TARGET`, `cd %VAR%`,
      `$(...)`. The tracker treats an unresolvable target as unknown and stops
      claiming the cwd is sealed, which fails open on purpose: guessing the
      other way denies writes in unrelated trees.

    Also inherent: an unrelated external project with its own top-level `app/`
    (a Next.js or Flask layout) is denied identically, because the check sees
    command text and has no cwd to disambiguate with.

    So this is a guard against the accident, not a sandbox against a determined
    process, and it should not be read as one. Closing the two classes above
    means enforcing the seal beneath the command parser — at the subprocess or
    filesystem layer, where the effective cwd and destination are known rather
    than inferred. `SEALED.md` and the generated `CLAUDE.md`/`AGENTS.md` at the
    tree root state the rule for everything this misses.
    """
    lower = cmd.lower()

    def _blocked_at(idx: int) -> bool:
        before = lower[max(0, idx - 80):idx]
        if _REDIRECT_RE.search(before):
            return True
        # `dd of=app/x` — the write is named by the operand, not by a verb, so
        # neither the redirect nor the verb list sees it. `if=` is a read and
        # deliberately not matched.
        if re.search(r"\bof\s*=\s*$", before):
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
        roots = ()
    for root in roots:
        for variant in (root.lower(), root.lower().replace("\\", "/")):
            idx = lower.find(variant)
            if idx >= 0 and _blocked_at(idx):
                return 26, "blocked"
    return _check_26_after_cd(cmd)


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
