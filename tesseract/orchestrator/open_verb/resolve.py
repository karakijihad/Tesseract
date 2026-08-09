"""Decide where a target opens: inside the cockpit, or out to the OS.

This is the only place that call is made. Everything downstream — the tool, the
MCP verb, the CLI — carries out a `Resolution` and never re-decides.

The rule is: inside when it can be, outside when it can't. "Can't" means the
page refuses framing, or the format is one the cockpit has no renderer for and
the operating system already opens perfectly well.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from tesseract.config.open_verb import OpenConfig
from tesseract.orchestrator.open_verb.asset_token import sign
from tesseract.orchestrator.open_verb import suffixes
from tesseract.orchestrator.open_verb.classify import Intent, TargetKind, classify
from tesseract.orchestrator.open_verb.probe import _redacted, probe_path, probe_url
from tesseract.paths import home_dir, install_root
from tesseract.permissions.path_validator import validate_path

Destination = Literal["canvas", "os"]
Handler = Literal["surface", "url", "launch", "app"]


class AmbiguousTarget(ValueError):
    """Path-shaped but absent — the caller is told, never guessed at."""


class RefusedTarget(ValueError):
    """A scheme or shape this verb will not act on."""


@dataclass(frozen=True)
class Resolution:
    destination: Destination
    handler: Handler
    reason: str
    canonical_target: str
    resolved_kind: str
    surface_type: str | None = None
    props: dict[str, Any] = field(default_factory=dict)
    # When set, `execute` reads this file's text into `props["text"]`. Reading
    # is a side effect bounded by `validate_path`, so it belongs in the
    # executing layer rather than here.
    text_from: str | None = None
    # Same reasoning for a folder card: `FolderRenderer` draws `props.entries`,
    # and without them the card is an empty box.
    list_dir: str | None = None


_IMAGE_SUFFIXES = suffixes.IMAGE
_VIDEO_SUFFIXES = suffixes.VIDEO
_AUDIO_SUFFIXES = suffixes.AUDIO
_TABLE_SUFFIXES = suffixes.TABLE
_MARKDOWN_SUFFIXES = suffixes.MARKDOWN
_HTML_SUFFIXES = suffixes.HTML
_CODE_SUFFIXES = suffixes.CODE
_LANGUAGE_BY_SUFFIX = suffixes.LANGUAGE_BY_SUFFIX


def _inside_read_boundary(path: Path) -> bool:
    ok, _reason = validate_path(
        str(path),
        write_root=str(home_dir()),
        read_root=str(install_root()),
        mode="read",
        resolve_symlinks=True,
    )
    return ok


def asset_href(path: str) -> str:
    """A signed reference to one file. The endpoint serves nothing without the
    signature, so this is the only way a local file reaches a canvas card —
    and the served set stays exactly the files that were opened."""
    target = str(path)
    return f"/api/asset?path={quote(target, safe='')}&sig={sign(target)}"


async def resolve(
    target: str,
    *,
    config: OpenConfig,
    intent: Intent = Intent.AUTO,
    destination: str = "auto",
) -> Resolution:
    """Decide what opening `target` means, and where it should end up.

    `intent` pins how the string is read (see `classify.Intent`).

    `destination` is a much narrower knob, and deliberately has no `canvas`
    value: where something lands is a capability, not a preference. The
    cockpit is already preferred wherever a renderer exists, and no request
    can conjure one for a `.exe`. The only choice a caller actually has is
    the opposite one — `"os"` means hand this to the program that owns it
    rather than rendering it, which is how you get a PDF into Acrobat.

    The override is applied to a fully resolved result, never in place of
    resolving. Everything that refuses — a secret file, a blocked network, a
    UNC path — raises before it is reached, so asking for `"os"` cannot open
    something that `"auto"` would have declined.
    """
    classification = classify(
        target,
        apps=config.apps,
        launch_extensions=config.launch_extensions,
        intent=intent,
    )
    kind = classification.kind

    if kind is TargetKind.REFUSED:
        raise RefusedTarget(classification.reason)
    if kind is TargetKind.AMBIGUOUS:
        raise AmbiguousTarget(classification.reason)

    if kind is TargetKind.APP:
        # `app`, not `launch`: an application IS an executable, and the launch
        # path refuses those absolutely. The gate for an app is the config
        # allowlist, not a file-type check.
        return Resolution(
            destination="os",
            handler="app",
            reason=f"launched {target}",
            canonical_target=classification.canonical,
            resolved_kind=str(kind),
        )

    if kind is TargetKind.QUERY:
        # A search phrase is always meant to be browsed. Probing the search
        # engine first would cost a round trip to learn what we already know.
        url = config.search_url.format(query=quote(classification.canonical, safe=""))
        return Resolution(
            destination="os",
            handler="url",
            reason=f"searched the web for {classification.canonical!r}",
            canonical_target=url,
            resolved_kind=str(kind),
        )

    if kind is TargetKind.PATH:
        resolved = _resolve_path(Path(classification.canonical), str(kind))
    else:
        resolved = await _resolve_url(classification.canonical, str(kind), config)
    return _to_os(resolved) if destination == "os" else resolved


def _to_os(resolved: Resolution) -> Resolution:
    """Send an already-resolved target out to the OS instead of the canvas.

    The canvas-only fields are dropped rather than carried: a `surface_type`
    or a `text_from` on an `os` result would describe a card nobody is going
    to build, and `execute` dispatches on `handler` alone.
    """
    if resolved.destination == "os":
        return resolved
    launching = resolved.resolved_kind == str(TargetKind.PATH)
    name = Path(resolved.canonical_target).name if launching else resolved.canonical_target
    return replace(
        resolved,
        destination="os",
        handler="launch" if launching else "url",
        reason=f"opened {name} outside the cockpit, as asked",
        surface_type=None,
        props={},
        text_from=None,
        list_dir=None,
    )


def _resolve_path(path: Path, kind: str) -> Resolution:
    probe = probe_path(path)
    name = path.name or str(path)

    # A card is persisted to canvas state and visible to anyone looking at the
    # Mirror, so rendering a secret is disclosure. Refused outright rather than
    # handed to the OS — "open my .env" should not silently succeed either way.
    # Credentials only. Memory, sessions and the journal are the operator's own
    # and they are the only person at the Mirror — refusing to show them their
    # own state would be paternalism, not security. A secret is different: it
    # would land in a persisted card AND in the assistant's context.
    if suffixes.is_secret(name):
        raise RefusedTarget(
            f"{name} holds credentials — it is not rendered and not opened"
        )

    # The asset endpoint serves only what is inside the read boundary. Without
    # this check a file outside it resolves to a card, reports "opened", and
    # renders a 404 — a lie the operator has to debug.
    if not _inside_read_boundary(path):
        # The cockpit cannot FETCH it — serving bytes over /api/asset is a read,
        # and reads are bounded. Handing it to the OS is not a read: the file
        # goes to another program on the operator's machine and TESSERACT sees
        # nothing of it. So a Desktop PDF opens in Adobe, as it always has.
        return Resolution(
            destination="os",
            handler="launch",
            reason=f"{name} lives outside the install, so the cockpit cannot "
            f"fetch it — opened it in the application that owns it",
            canonical_target=str(path),
            resolved_kind=kind,
        )

    def inside(surface_type: str, **extra: Any) -> Resolution:
        props: dict[str, Any] = {"url": asset_href(str(path))} | extra
        return Resolution(
            destination="canvas",
            handler="surface",
            reason=f"opened {name} in the cockpit",
            canonical_target=str(path),
            resolved_kind=kind,
            surface_type=surface_type,
            props=props,
        )

    def inside_text(surface_type: str, **extra: Any) -> Resolution:
        return Resolution(
            destination="canvas",
            handler="surface",
            reason=f"opened {name} in the cockpit",
            canonical_target=str(path),
            resolved_kind=kind,
            surface_type=surface_type,
            props=dict(extra),
            text_from=str(path),
        )

    if probe.is_dir:
        return Resolution(
            destination="canvas",
            handler="surface",
            reason=f"opened the folder {name} in the cockpit",
            canonical_target=str(path),
            resolved_kind=kind,
            surface_type="folder",
            props={"root": str(path)},
            list_dir=str(path),
        )

    suffix = probe.suffix
    if suffix == ".pdf":
        return inside("pdf")
    if suffix in _IMAGE_SUFFIXES:
        return inside("image")
    if suffix in _VIDEO_SUFFIXES:
        return inside("video")
    if suffix in _AUDIO_SUFFIXES:
        return inside("audio")
    if suffix in _TABLE_SUFFIXES:
        return inside_text("table", delimiter="\t" if suffix == ".tsv" else ",")
    if suffix in _MARKDOWN_SUFFIXES:
        return inside_text("markdown")
    if suffix in _HTML_SUFFIXES:
        return inside_text("html")
    if suffix in _CODE_SUFFIXES:
        return inside_text("code", language=_LANGUAGE_BY_SUFFIX.get(suffix, "text"))

    return Resolution(
        destination="os",
        handler="launch",
        reason=f"the cockpit has no renderer for {suffix or 'this file type'} — "
        f"opened {name} in the application that owns it",
        canonical_target=str(path),
        resolved_kind=kind,
    )


async def _resolve_url(url: str, kind: str, config: OpenConfig) -> Resolution:
    probe = await probe_url(
        url,
        timeout_s=config.probe_timeout_s,
        blocked_networks=config.blocked_networks,
    )
    if probe.blocked:
        raise RefusedTarget(
            f"{_redacted(probe.final_url or url)} resolves into a network this "
            f"will not reach"
        )

    ctype = probe.content_type
    final = probe.final_url or url

    def inside(surface_type: str) -> Resolution:
        return Resolution(
            destination="canvas",
            handler="surface",
            reason=f"opened {_redacted(final)} in the cockpit",
            canonical_target=final,
            resolved_kind=kind,
            surface_type=surface_type,
            props={"url": final},
        )

    def outside(why: str) -> Resolution:
        return Resolution(
            destination="os",
            handler="url",
            reason=why,
            canonical_target=final,
            resolved_kind=kind,
        )

    if ctype.startswith("image/"):
        return inside("image")
    if ctype.startswith("video/"):
        return inside("video")
    if ctype.startswith("audio/"):
        return inside("audio")

    # A remote PDF renders in the frame's built-in viewer when the host permits
    # framing. Fetching it through pdf.js instead would need CORS the origin
    # has no reason to grant.
    if ctype == "application/pdf" or ctype.startswith("text/") or ctype == "application/json":
        if probe.frameable:
            return inside("webview")
        return outside(
            f"{_redacted(final)} refuses to be embedded — opened it in your browser"
        )

    if not ctype and probe.status == 0:
        return outside(f"could not reach {_redacted(final)} to check — opened it in your browser")

    return outside(
        f"{ctype or 'that content type'} is not something the cockpit renders — "
        f"opened {_redacted(final)} in your browser"
    )
