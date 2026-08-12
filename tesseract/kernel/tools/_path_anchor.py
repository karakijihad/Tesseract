"""Anchor a relative path for the read-side file tools.

`file_write` resolves every relative path against `home_dir()` — the state
root — and the runtime lockdown denies the source prefixes from there. The
read tools resolved against `ToolContext.workspace_root` — the CODE tree —
and the two roots are never the same directory: `<home>/app` vs `<home>` in a
packaged install, `<repo>` vs `<repo>/tesseract` in a dev checkout.

So a write and a read of the same string named different files. `file_write`
put `downloads/paper.pdf` under the state root and reported that path back;
`file_read("downloads/paper.pdf")` then looked under the code tree and said
the file did not exist. Every state directory the operator can be handed a
path to — `downloads/`, `uploads/`, `vault/`, `workshop/` — was unreachable by
the relative path the runtime itself had just printed.

The code tree stays the primary anchor: reading source by a repo-relative path
is the common case and must not change. The state root is consulted only for
the paths a written artifact can actually land in
(`paths.READABLE_STATE_PREFIXES`), and only when the code tree has no such
entry — so this can surface a file that was previously unreachable, but can
never shadow one that was already found.

The state-dir bound is load-bearing, not tidiness. `permissions.yaml` carries
`path_overrides` for `file_write` ONLY; every read tool is a flat posture with
no path scoping under it. An unbounded second anchor would therefore make
`file_read(".env")` — which resolves to nothing under the code tree today —
return the operator's API keys from the state root, with no policy layer in
between to refuse it.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath


class ReadPathRefused(Exception):
    """A read path was refused by policy, as distinct from not being found.

    Separate from "not found" on purpose: a refusal that reported itself as a
    missing file would teach the assistant to go looking for the same content
    by another route, and would hide a real policy decision behind what reads
    like a typo.
    """


def _is_drive_relative(raw: str) -> bool:
    """Whether `raw` names a drive without anchoring to its root (`C:model.onnx`).

    Windows resolves this against the process's per-drive current directory,
    so it is NOT `is_absolute()` — yet joining it onto a base discards that
    base entirely. `PureWindowsPath` explicitly, so the check holds when the
    suite runs on a POSIX host.
    """
    pure = PureWindowsPath(raw)
    return bool(pure.drive) and not pure.root


def within_root(candidate: Path, root: Path) -> bool:
    """Whether `candidate` resolves to somewhere under `root`."""
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def anchor_read_path(raw: str, workspace_root: str) -> Path:
    """Resolve `raw` for a read-side tool.

    Raises `ReadPathRefused` when the path names a credential-bearing file, or
    when a relative path escapes `workspace_root` instead of staying under it.

    Absolute paths skip the containment check. That is deliberate and not an
    oversight: diagnosis work reads `runtime/logs/backend/**`, which no
    relative anchor reaches, and the operator's standing direction is that
    reading is not the dangerous half. What they do NOT skip is the secrets
    refusal, which applies to every path however it arrived.

    A relative path anchors at `workspace_root`. When nothing exists there and
    the path sits under a readable state prefix, the state root is tried
    instead. When neither holds it, the `workspace_root` candidate is returned
    so the caller's "not found" message names the root the caller asked about.

    An existing path comes back RESOLVED; a missing one comes back as written.
    Both halves are deliberate — the first closes the link-swap window, the
    second keeps "not found" legible.
    """
    candidate = _anchor(raw, workspace_root)

    refuse_if_secret_path(Path(raw))
    # A link whose own name is innocuous still opens whatever it points at, so
    # the TARGET is checked too — and by component, not by leaf. A link to
    # `.env/keys.txt` resolves to a leaf named `keys.txt`, so a leaf-only check
    # on the target reads as safe while opening a file inside a secret
    # directory.
    try:
        real = candidate.resolve()
    except (OSError, RuntimeError):
        return candidate
    refuse_if_secret_path(real)

    # The RESOLVED path is returned, not the one that was checked — the same
    # reasoning the state-prefix branch below already records. Handing back the
    # unresolved form lets a link be repointed between this check and the
    # caller's open, so the path validated and the path read are two different
    # files. Every caller re-opens by path (`file_read` reads it, `grep` hands
    # it to a subprocess), so the window is real rather than theoretical.
    return real if real.exists() else candidate


def _refuse_if_secret(name: str) -> None:
    from tesseract.paths import is_secret_filename

    if is_secret_filename(name):
        raise ReadPathRefused(
            f"{name} holds credentials and is not readable through the file "
            f"tools. Key PRESENCE is reported by Settings → API keys; key "
            f"VALUES are not readable by design."
        )


def refuse_if_secret_path(path: Path) -> None:
    """Refuse if ANY component of `path` is credential-bearing.

    Component-wise rather than leaf-only, because `.env/keys.txt` reduces to
    `keys.txt` and a directory is as good a hiding place as a name.

    Applied to fully-resolved absolute paths too, which means a machine whose
    install sits under a directory named `.env` (or one ending `.pem`) can read
    nothing at all. That is the deliberate direction of the trade: the
    false-positive needs a pathological install layout, the false-negative
    hands out credentials, and this is an AUTO-posture tool with no prompt
    between it and the caller.
    """
    for part in path.parts:
        _refuse_if_secret(part)


def _anchor(raw: str, workspace_root: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    if _is_drive_relative(raw):
        raise ReadPathRefused(
            f"{raw!r} is drive-relative — it names a location on that drive "
            f"rather than a path under the workspace."
        )

    root = Path(workspace_root)
    primary = root / path
    # Resolve before use. `vault/../../.env` is a relative path that climbs
    # out of the workspace entirely, and the join alone does not notice.
    if not within_root(primary, root):
        raise ReadPathRefused(
            f"{raw!r} resolves outside the workspace. Pass an absolute path "
            f"if you meant to read somewhere else."
        )
    if primary.exists():
        return primary

    from tesseract.paths import home_dir, readable_state_prefix

    prefix = readable_state_prefix(path.as_posix())
    if prefix is None:
        return primary

    # The name decides whether to look; containment decides whether to return.
    # Two containment checks, and both are load-bearing:
    #
    #   state root inside home — `<home>/workshop` could be a junction pointing
    #     out of the install, and `.resolve()` follows it, so without this a
    #     path "under workshop" satisfies the second check while sitting
    #     anywhere on disk.
    #   target inside state root — `workshop/../.env` also starts with a state
    #     prefix and would otherwise resolve to `<home>/.env`.
    try:
        home = home_dir().resolve()
        state_root = (home_dir() / prefix).resolve()
        state_root.relative_to(home)
        resolved = (home_dir() / path).resolve()
        resolved.relative_to(state_root)
    except (OSError, RuntimeError, ValueError):
        return primary
    # The RESOLVED path is returned, not the path that was checked: handing back
    # the unresolved form would let a symlink inside the state tree be repointed
    # between this check and the caller's open, so the path validated and the
    # path read would be two different files.
    return resolved if resolved.exists() else primary


__all__ = ["ReadPathRefused", "anchor_read_path", "refuse_if_secret_path", "within_root"]
