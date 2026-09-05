"""The door a playbook's text goes through: every path it names, judged.

A playbook is prose the assistant reads and follows, written by a person or
a model, so a path in it is an instruction to open something. This module
finds every path-shaped token in that prose and says, for each, whether it
may be written down: not if it names a credential-bearing file, and not if
it cannot be shown to sit inside the home tree.

**Why a module and not a regex.** The first door was one expression and a
`Path.is_absolute()` call, and each audit pass found a shape it did not
know: a UNC share, a shell variable, a root with one component, a `file:`
URI, a drive path on POSIX, a drive-relative path, a current-drive root, a
traversal. Each patch answered the finding in front of it and dropped a
property the last patch had. So the properties are written here together
and every rule below is checked against all of them:

1. **Every shape is a token.** POSIX absolute (`/etc`), tilde (`~/.ssh`),
   drive (`C:\\x`, `C:/x`), drive-relative (`C:x`), current-drive root
   (`\\x`), UNC (`\\\\host\\share`), the extended-length and device forms
   (`\\\\?\\UNC\\host\\share`, `\\\\?\\C:\\x`, `\\\\.\\device`), shell variable
   (`$HOME/x`, `${HOME}/x`), Windows variable (`%USERPROFILE%\\x`), a `file:`
   URI with or without a host, and a relative path with a separator in it,
   traversal included.
2. **Judged by the string, never by the host.** A drive path is a drive
   path on POSIX too; `Path.resolve()` is not consulted and the user's home
   is not asked for, so the answer is the same on every machine and
   testable on any.
3. **What cannot be shown inside home is outside.** A UNC share, an
   extended-length or device path, a tilde, a variable, a drive-relative or
   current-drive path resolve against state this door cannot see; they are
   refused, and the refusal says to write the path relative.
4. **A relative path is judged too.** It is taken against the home tree, and
   one that climbs out with `..` is outside.
5. **A URL is not a path**, except a `file:` URI, which is the path it names.
6. **A root needs a boundary.** `<slug>/README.md` holds no rooted path; the
   `/` continues a relative one that `<` and `>` cut short, and `and/or` is
   a word.
7. **Prose stays prose.** A bare `~` or `$5` is not a path; a root needs at
   least one component after it.

The credential check is `paths.secret_path_component`, the same walk the
read tools and the permission gate use, so this door cannot come to refuse
a different set.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote

from tesseract.paths import secret_path_component

#: What ends a token: whitespace, quotes, brackets and the punctuation prose
#: puts after a path.
_END = r"""[^\s"'`<>()\[\]{},;]"""
#: What may precede a root for it to count as one: the start of the text,
#: whitespace, or an opener. Anything else means the separator continues a
#: word, as in `and/or` or `<slug>/README.md`.
_BOUNDARY = r"""(?<![^\s"'`(\[{,;:=])"""

_TOKEN_RE = re.compile(
    "|".join((
        rf"{_BOUNDARY}\\\\[?.]\\{_END}+",              # \\?\UNC\host\share  \\?\C:\x  \\.\device
        rf"{_BOUNDARY}\\\\[\w.-]+\\{_END}+",           # \\host\share\x
        rf"{_BOUNDARY}[A-Za-z]:{_END}+",                # C:\x  C:/x  C:x
        rf"{_BOUNDARY}\\[\w.~-]{_END}*",               # \Windows\x (current drive)
        rf"{_BOUNDARY}(?:~/|/){_END}+",                # /etc  ~/.ssh
        rf"{_BOUNDARY}\$\{{?\w+\}}?[\\/]{_END}*",       # $HOME/x  ${HOME}/x
        rf"{_BOUNDARY}%\w+%[\\/]{_END}*",              # %USERPROFILE%\x
        r"[\w.~-]+(?:[\\/][\w.~-]+)+",                 # workshop/projects/x  ../x
    ))
)
_FILE_URI_RE = re.compile(r"file://(?P<host>[^/\s]*)(?P<path>/[^\s]*)", re.IGNORECASE)
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

CREDENTIAL = "Refused: a step names a credential-bearing path ({offender!r})."
OUTSIDE = (
    "Refused: a step names a path outside the home tree, or one that cannot "
    "be shown to be inside it (a network share, a variable, a tilde, a "
    "drive-relative path). Write the path relative to the home tree instead."
)


def scan(text: str) -> list[str]:
    """Every path-shaped token in `text`, `file:` URIs as the paths they name."""
    prepared = _URL_RE.sub(" ", _FILE_URI_RE.sub(_file_uri_as_path, text))
    return _TOKEN_RE.findall(prepared)


def judge(token: str, home: Path) -> str | None:
    """Why `token` may not be written into a playbook, or None."""
    offender = secret_path_component(token.lstrip("\\/"))
    if offender:
        return CREDENTIAL.format(offender=offender)
    if token.startswith(("\\\\", "$", "%")):
        return OUTSIDE  # a share or a variable: resolved by state this door cannot see
    if token.startswith("\\"):
        return OUTSIDE  # rooted on whichever drive is current
    if _DRIVE_RE.match(token):
        if len(token) < 3 or token[2] not in "\\/":
            return OUTSIDE  # drive-relative: `C:x` depends on that drive's cwd
        return None if _inside(token, home) else OUTSIDE
    if token.startswith("~/"):
        return OUTSIDE  # the user's home is the host's answer, not this door's
    if token.startswith("/"):
        return None if _inside(token, home) else OUTSIDE
    # Relative: taken against the home tree, so `../x` climbs out of it.
    joined = _norm(home) + "/" + _norm(token)
    return None if _inside(joined, home) else OUTSIDE


def refuse_paths(texts: list[str], home: Path) -> str | None:
    """The first refusal across `texts`, or None when every path may stay."""
    for text in texts:
        for token in scan(text):
            why = judge(token, home)
            if why:
                return why
    return None


def _file_uri_as_path(match: re.Match) -> str:
    host = match.group("host")
    path = unquote(match.group("path"))
    if host and host.lower() != "localhost":
        return " \\\\" + host + path.replace("/", "\\") + " "
    # file:///C:/x names a drive; file:///etc/x a POSIX root.
    stripped = path.lstrip("/")
    if _DRIVE_RE.match(stripped):
        return " " + stripped + " "
    return " /" + stripped + " "


def _norm(raw: "str | Path") -> str:
    """One spelling for comparison: forward slashes, `.` and `..` folded,
    no trailing slash, and drive letters case-folded because a drive is."""
    text = str(raw).replace("\\", "/")
    folded = posixpath.normpath(text)
    if _DRIVE_RE.match(folded):
        folded = folded[0].lower() + folded[1:]
        folded = folded.lower() if os.name == "nt" else folded
    return folded.rstrip("/") or folded


def _inside(candidate: "str | Path", home: Path) -> bool:
    root, path = _norm(home), _norm(candidate)
    if path.startswith("..") and not path.startswith(root):
        return False
    return path == root or path.startswith(root + "/")


__all__ = ["CREDENTIAL", "OUTSIDE", "judge", "refuse_paths", "scan"]
