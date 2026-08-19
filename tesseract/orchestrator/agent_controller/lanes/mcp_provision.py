"""Auto-provisions MCP hub connectivity for ``claude``/``codex`` so a spawned
or hand-launched CLI wakes up already connected to the embedded hub
(``tesseract/config/mcp.yaml``).

Both CLIs are provisioned at **user scope** — one file each, applying in every
directory. Project scope was the original choice for Claude Code, and it tied
connectivity to the directory the pane happened to open in: a lane or an
operator who moved somewhere else before launching got a CLI with no hub at
all. What actually distinguishes "launched inside TESSERACT" from any other
shell is the bearer token in the environment, which only the runtime's own
child processes inherit — so directory scope bought nothing and cost reach.

Claude Code: merges ``~/.claude.json`` ``mcpServers.tesseract`` — HTTP
transport, env-expanded ``Authorization`` header (``${VAR}`` syntax).
Codex: merges ``~/.codex/config.toml`` ``[mcp_servers.tesseract]`` — ``url`` +
``bearer_token_env_var`` (native env indirection, no literal secret).

Neither file is ours. ``~/.claude.json`` in particular holds a live session's
own state and is rewritten by it constantly, so both writers no-op when the
entry already matches: provisioning touches disk when the endpoint or identity
actually changes, and never again. When it does write, it re-reads immediately
before the replace and starts over if the bytes moved under it — the in-process
lock says nothing about a separate ``claude`` process, and a full-file rewrite
over someone else's newer state would silently drop it. That check narrows the
window to the gap between the last read and the ``os.replace``; it does not
close it. Closing it would need an OS-level file lock the stdlib does not offer
portably, and the remaining window is microseconds against a file written every
few seconds.

Stale project-scope ``.mcp.json`` files written by the previous scheme are
removed when they carry one of our own client identities. Claude Code resolves
a duplicate server name by scope — local, then project, then user, taking the
whole entry rather than merging — so a leftover project file silently shadows
what we just provisioned. Foreign entries and foreign files are left alone.

Config-as-authority: raises if the resolved client's token env var is unset
at provision time — a dead hub connection is caught at spawn, not first use.
Idempotent; never touches unrelated keys in either file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from tesseract.config.mcp import MCPConfig

from .models import LaneKind

log = logging.getLogger(__name__)

ProvisionKind = LaneKind | Literal["terminal"]

_CLIENT_NAME_BY_KIND: dict[str, str] = {
    "claude": "lane-claude",
    "codex": "lane-codex",
    "terminal": "terminal-manual",
}

def ensure_runtime_tokens(mcp_cfg: MCPConfig) -> list[str]:
    """Mint the identities the runtime issues to its own child processes.

    These are not credentials anyone types. Every process this app spawns is
    handed exactly one of them and stripped of the rest, so ownership on the
    lane surface means something and a spawned CLI cannot come back in
    holding the operator's bearer. That only works if they EXIST, and
    ``provision`` refuses outright when one is unset — "refusing to provision
    a dead hub connection" — so on a fresh install the terminal reached no
    hub at all until somebody hand-generated three secrets they had no way to
    know they needed. ``mcp.yaml`` called them auto-provisioned for a year
    while nothing provisioned them.

    Written to ``.env`` AND into this process's environment, because
    provisioning happens later in the same boot and a value only on disk
    would not be read until the next one.

    The operator client is deliberately not here. That one is a decision —
    it is what lets something outside this machine's app talk in, so it stays
    a button the operator presses.
    """
    import os

    from tesseract import env_file

    runtime_names = set(_CLIENT_NAME_BY_KIND.values())
    on_disk = env_file.read_values()
    minted: dict[str, str] = {}
    for client in mcp_cfg.clients:
        if client.name not in runtime_names:
            continue
        if (os.environ.get(client.token_env) or "").strip():
            continue
        existing = (on_disk.get(client.token_env) or "").strip()
        if existing:
            # On disk but not in this environment — seeded by a previous boot
            # and never loaded, or written by the operator by hand. Adopt it
            # rather than replacing it: a rotation nobody asked for would
            # orphan whatever is already holding the old value.
            os.environ[client.token_env] = existing
            continue
        minted[client.token_env] = env_file.generate_token()

    if not minted:
        return []
    try:
        env_file.set_values(minted)
    except OSError:
        log.exception("mcp: could not write runtime tokens to .env")
        return []
    os.environ.update(minted)
    # Names only, never values — this log ships inside bug reports.
    log.info("mcp: minted runtime identities %s", ", ".join(sorted(minted)))
    return sorted(minted)


_CLAUDE_CONFIG_NAME = ".claude.json"
_PROJECT_CONFIG_NAME = ".mcp.json"
_CODEX_BLOCK_HEADER = "[mcp_servers.tesseract]"
# Bounded retries for the re-read-before-replace check. A live claude session
# writing continuously should lose the race a couple of times at most; past
# that, skipping is safer than forcing an overwrite.
_CLAUDE_WRITE_ATTEMPTS = 3
# Brief pause between attempts so a retry does not land in the same rename
# window that lost the previous one.
_CLAUDE_RETRY_PAUSE_S = 0.01
# Each CLI's config is a single global file that every lane, delegate, and
# terminal provision call reads-modifies-writes, and those run in parallel
# (asyncio.to_thread). One lock per file so concurrent callers can't interleave
# a torn read/write or race the identity-precedence snapshot.
_CODEX_CONFIG_LOCK = threading.Lock()
_CLAUDE_CONFIG_LOCK = threading.Lock()


def provision(
    kind: ProvisionKind,
    mcp_cfg: MCPConfig,
    *,
    cleanup_dirs: Sequence[Path] = (),
) -> bool:
    """Ensure the hub is reachable from the operator's claude and/or codex
    config. ``kind="terminal"`` provisions both, since a hand-launched
    terminal may run either CLI.

    Identity precedence: a terminal provision never overwrites an existing
    LANE identity. Both CLIs' configs are global, so lane and terminal write
    the same key, and last-writer-wins would re-identify every live lane as
    ``terminal-manual``. Lane provisions still overwrite freely (lane wins).

    The flip side of global scope is that two claude identities cannot be held
    at once — a lane and a manual terminal share one entry. Codex has always
    worked this way; claude now matches.

    ``cleanup_dirs`` are directories the old project-scope scheme may have
    written a ``.mcp.json`` into — a caller passes the working directory it
    knows about, because that is exactly where the previous code would have
    put one. Removing it is what makes the user-scope entry authoritative.

    Returns whether the **claude** config reached the state this call wanted —
    not whether the CLI can reach the hub, and not a statement about codex.
    The codex writer has no decline path: it writes or raises, so it is
    represented by this function returning at all. False means back off and
    try again later; a caller that caches "provisioned" must not cache on
    False, or it pins itself to a hub it never reached."""
    client_name = _CLIENT_NAME_BY_KIND.get(kind)
    if client_name is None:
        raise RuntimeError(f"mcp_provision: unknown kind {kind!r}")
    client = next((c for c in mcp_cfg.clients if c.name == client_name), None)
    if client is None:
        raise RuntimeError(
            f"mcp_provision: mcp.yaml has no client named {client_name!r} (kind={kind!r})"
        )
    if not os.environ.get(client.token_env):
        raise RuntimeError(
            f"mcp_provision: required env var {client.token_env!r} for client "
            f"{client_name!r} is unset — refusing to provision a dead hub connection"
        )

    url = f"http://{mcp_cfg.server.host}:{mcp_cfg.server.port}/mcp"
    preserve_envs: frozenset[str] = frozenset()
    if kind == "terminal":
        preserve_envs = frozenset(
            c.token_env
            for c in mcp_cfg.clients
            if c.name in _CLIENT_NAME_BY_KIND.values() and c.name != client_name
        )

    claude_ok = True
    if kind in ("claude", "terminal"):
        # Only reclaim the project-scope files once user scope is known good.
        # Deleting the shadow before the thing it shadows is in place would
        # leave a CLI with no hub entry at all.
        claude_ok = _provision_claude_user_config(url, client.token_env, preserve_envs)
        if claude_ok:
            our_token_envs = frozenset(
                c.token_env for c in mcp_cfg.clients if c.name in _CLIENT_NAME_BY_KIND.values()
            )
            for directory in cleanup_dirs:
                _remove_stale_project_config(Path(directory), our_token_envs)
    if kind in ("codex", "terminal"):
        _provision_codex_config_toml(url, client.token_env, preserve_envs)
    return claude_ok


def _auth_header_env(entry: object) -> str | None:
    """Extract the ``${VAR}`` name from a claude MCP server entry's
    Authorization header, or None when the shape doesn't match."""
    if not isinstance(entry, dict):
        return None
    headers = entry.get("headers")
    if not isinstance(headers, dict):
        return None
    match = re.fullmatch(r"Bearer \$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(headers.get("Authorization") or ""))
    return match.group(1) if match else None


def _provision_claude_user_config(
    url: str, token_env: str, preserve_envs: frozenset[str] = frozenset()
) -> bool:
    """Whether the user-scope entry is in the state this call wanted.

    False means we backed off rather than overwrite a file that kept moving —
    the entry may be absent or stale, so callers must not act as though the hub
    is reachable.
    """
    # Hold the lock across the whole read-modify-write so the identity snapshot
    # and the write are atomic w.r.t. concurrent provisions in this process.
    with _CLAUDE_CONFIG_LOCK:
        path = Path.home() / _CLAUDE_CONFIG_NAME
        last_error: Exception | None = None
        for attempt in range(_CLAUDE_WRITE_ATTEMPTS):
            if attempt:
                time.sleep(_CLAUDE_RETRY_PAUSE_S)
            # Cleared per attempt: what the give-up message should describe is
            # why the LAST attempt failed. Carrying an earlier read error
            # forward would report a hard failure for a run that ended in
            # ordinary contention, and vice versa.
            last_error = None
            try:
                before = _read_bytes(path)
                existing = _parse_claude_config(path, before)
            except (OSError, ValueError) as exc:
                # Two transients of a file being written right now: Windows
                # denies a read that lands inside another process's rename, and
                # a read that lands mid-write parses as nothing. Both are what
                # the loop is for; only a persistent one is a real error.
                last_error = exc
                continue
            servers = existing.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise RuntimeError(f"mcp_provision: {path} 'mcpServers' is not an object")
            current = servers.get("tesseract")
            current_env = _auth_header_env(current)
            if isinstance(current, dict) and current.get("url") == url and current_env == token_env:
                return True  # already provisioned by this exact client — no-op
            if current_env in preserve_envs:
                return True  # existing lane identity wins over a terminal provision
            servers["tesseract"] = {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer ${{{token_env}}}"},
            }
            # A live claude session rewrites this file on its own schedule and
            # the lock above cannot see it. Re-read at the last moment: if the
            # bytes moved since `before`, our merge is against stale state and
            # replacing would drop whatever it just wrote.
            try:
                moved = _read_bytes(path) != before
            except OSError as exc:
                # Denied mid-rename: treat as moved and retry, but record it —
                # a read that keeps failing here is the same hard failure as
                # one that fails above, and reporting it as contention sends
                # the operator looking for a busy file rather than a broken one.
                last_error = exc
                continue
            if moved:
                continue
            _atomic_write(path, json.dumps(existing, indent=2) + "\n")
            return True
        # Contention and a genuinely broken file end the same way, but they
        # need different words: one resolves itself, the other never will and
        # the operator is the only one who can fix it.
        if last_error is None:
            log.warning(
                "mcp_provision: %s kept changing under us — leaving it to the "
                "next provision rather than overwriting a live session's state",
                path,
            )
        else:
            log.warning(
                "mcp_provision: %s could not be read after %d attempts (%s: %s) "
                "— the hub connection is NOT provisioned; this will not resolve "
                "on its own if the file is corrupt, locked, or unreadable",
                path,
                _CLAUDE_WRITE_ATTEMPTS,
                type(last_error).__name__,
                last_error,
            )
        return False


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _parse_claude_config(path: Path, raw: bytes | None) -> dict:
    if raw is None or not raw.strip():
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"mcp_provision: {path} did not parse to a JSON object")
    return parsed


def _remove_stale_project_config(working_dir: Path, our_token_envs: frozenset[str]) -> None:
    """Drop a project-scope ``.mcp.json`` we wrote under the old scheme.

    Project scope outranks user scope and replaces the entry wholesale, so a
    leftover file silently pins the CLI to whatever identity it was last
    provisioned with. Only a file whose ``tesseract`` entry names one of our
    own client token env vars is ours to remove; anything else — a foreign
    server, a hand-authored entry, extra keys the operator added — stays.
    """
    path = working_dir / _PROJECT_CONFIG_NAME
    raw = _read_bytes(path)
    if raw is None:
        return
    try:
        parsed = _parse_claude_config(path, raw)
    except (RuntimeError, ValueError, UnicodeDecodeError):
        return  # not ours to interpret, so not ours to delete
    servers = parsed.get("mcpServers")
    if not isinstance(servers, dict):
        return
    if _auth_header_env(servers.get("tesseract")) not in our_token_envs:
        return
    if set(parsed) - {"mcpServers"} or set(servers) - {"tesseract"}:
        return  # carries content beyond what we wrote — leave it for the operator
    try:
        path.unlink()
    except OSError:
        log.warning("mcp_provision: could not remove stale %s", path, exc_info=True)


def _provision_codex_config_toml(
    url: str, token_env: str, preserve_envs: frozenset[str] = frozenset()
) -> None:
    with _CODEX_CONFIG_LOCK:
        codex_dir = Path.home() / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        path = codex_dir / "config.toml"
        existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
        parsed = tomllib.loads(existing_text) if existing_text.strip() else {}
        section = parsed.get("mcp_servers")
        current = section.get("tesseract") if isinstance(section, dict) else None
        if (
            isinstance(current, dict)
            and current.get("url") == url
            and current.get("bearer_token_env_var") == token_env
        ):
            return  # already provisioned by this exact client — no-op
        if (
            isinstance(current, dict)
            and current.get("bearer_token_env_var") in preserve_envs
        ):
            # Existing lane identity wins over a terminal provision — the
            # codex config is GLOBAL, so one pane open must not re-identify
            # every future codex lane turn (W0 audit D4).
            return

        block = (
            f"{_CODEX_BLOCK_HEADER}\n"
            f'url = "{url}"\n'
            f'bearer_token_env_var = "{token_env}"\n'
        )
        lines = existing_text.splitlines(keepends=True)
        start = next(
            (i for i, ln in enumerate(lines) if ln.strip() == _CODEX_BLOCK_HEADER), None
        )
        if start is not None:
            end = start + 1
            while end < len(lines) and not lines[end].lstrip().startswith("["):
                end += 1
            new_text = "".join(lines[:start]) + block + "".join(lines[end:])
        elif existing_text.strip():
            new_text = existing_text.rstrip("\n") + "\n\n" + block
        else:
            new_text = block
        _atomic_write(path, new_text)


def _sweep_own_temps(path: Path) -> None:
    """Remove temp files a previous write of ``path`` left behind.

    The write path unlinks its own staging file on any raised exception, but a
    SIGKILL or power loss between ``mkstemp`` and ``os.replace`` leaves one on
    disk. That debris now lands in the operator's home directory, where nothing
    else sweeps it and a stray ``.claude.json.*`` reads like a backup worth
    restoring. Only files matching this exact staging pattern are touched.

    Safe to sweep unconditionally only because every ``_atomic_write`` for a
    given target runs under that target's lock — so no other thread can be
    mid-stage on the same path when this runs.
    """
    try:
        for stale in path.parent.glob(f"{path.name}.*.tmp"):
            if stale.is_file():
                stale.unlink()
    except OSError:
        pass  # best-effort tidying; never fails the write it precedes


def _atomic_write(path: Path, content: str) -> None:
    # A unique temp per write (was a single shared ``<path>.tmp``) so two
    # concurrent writers to the same target can't collide on the temp file
    # (WinError 32) or clobber each other's staging. Same-dir temp keeps the
    # os.replace atomic. Callers serialize per path; this is defense in depth.
    _sweep_own_temps(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # Windows can transiently deny the replace when an indexer/AV briefly
        # holds the just-created target; retry within a bounded budget before
        # giving up (only kicks in under contention — the normal path replaces
        # once with no sleep).
        for attempt in range(20):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.02)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = ["provision", "ProvisionKind"]
