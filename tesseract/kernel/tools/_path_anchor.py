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

from pathlib import Path


def anchor_read_path(raw: str, workspace_root: str) -> Path:
    """Resolve `raw` for a read-side tool.

    Absolute paths pass through unchanged — bounding those is the permission
    layer's job, not this function's, and it was never this function's before.

    A relative path anchors at `workspace_root`. When nothing exists there and
    the path sits under a readable state prefix, the state root is tried
    instead. When neither holds it, the `workspace_root` candidate is returned
    so the caller's "not found" message names the root the caller asked about.
    """
    path = Path(raw)
    if path.is_absolute():
        return path

    primary = Path(workspace_root) / path
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


__all__ = ["anchor_read_path"]
