"""Decide where a target opens: inside the cockpit, or out to the OS.

This is the only place that call is made. Everything downstream — the tool, the
MCP verb, the CLI — carries out a `Resolution` and never re-decides.

The rule is: inside when it can be, outside when it can't. "Can't" means the
page refuses framing, or the format is one the cockpit has no renderer for and
the operating system already opens perfectly well.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from tesseract.config.open_verb import OpenConfig
from tesseract.orchestrator.open_verb.asset_token import sign
from tesseract.orchestrator.open_verb.classify import TargetKind, classify
from tesseract.orchestrator.open_verb.probe import probe_path, probe_url

Destination = Literal["canvas", "os"]
Handler = Literal["surface", "url", "launch"]


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


_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".m4v", ".ogv"})
_AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac"})
_TABLE_SUFFIXES = frozenset({".csv", ".tsv"})
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_HTML_SUFFIXES = frozenset({".html", ".htm"})
_CODE_SUFFIXES = frozenset(
    {
        ".txt", ".log", ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
        ".java", ".rb", ".c", ".h", ".cpp", ".hpp", ".cs", ".sh", ".yaml",
        ".yml", ".toml", ".ini", ".cfg", ".json", ".xml", ".css", ".sql",
    }
)

_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
    ".jsx": "jsx", ".rs": "rust", ".go": "go", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".sh": "bash", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".json": "json", ".xml": "xml", ".css": "css", ".sql": "sql",
}


def asset_href(path: str) -> str:
    """A signed reference to one file. The endpoint serves nothing without the
    signature, so this is the only way a local file reaches a canvas card —
    and the served set stays exactly the files that were opened."""
    target = str(path)
    return f"/api/asset?path={quote(target, safe='')}&sig={sign(target)}"


async def resolve(target: str, *, config: OpenConfig) -> Resolution:
    classification = classify(
        target, apps=config.apps, launch_extensions=config.launch_extensions
    )
    kind = classification.kind

    if kind is TargetKind.REFUSED:
        raise RefusedTarget(classification.reason)
    if kind is TargetKind.AMBIGUOUS:
        raise AmbiguousTarget(classification.reason)

    if kind is TargetKind.APP:
        return Resolution(
            destination="os",
            handler="launch",
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
        return _resolve_path(Path(classification.canonical), str(kind))

    return await _resolve_url(classification.canonical, str(kind), config)


def _resolve_path(path: Path, kind: str) -> Resolution:
    probe = probe_path(path)
    name = path.name or str(path)

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
    probe = await probe_url(url, timeout_s=config.probe_timeout_s)
    ctype = probe.content_type
    final = probe.final_url or url

    def inside(surface_type: str) -> Resolution:
        return Resolution(
            destination="canvas",
            handler="surface",
            reason=f"opened {final} in the cockpit",
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
            f"{final} refuses to be embedded — opened it in your browser"
        )

    if not ctype and probe.status == 0:
        return outside(f"could not reach {final} to check — opened it in your browser")

    return outside(
        f"{ctype or 'that content type'} is not something the cockpit renders — "
        f"opened {final} in your browser"
    )
