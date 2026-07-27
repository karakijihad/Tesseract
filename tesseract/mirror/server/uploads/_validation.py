from __future__ import annotations

import re
from pathlib import Path

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_SEGMENT_MAX = 128

_EXT_MIME = {
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".webp": "image/webp",
    # Audio — uploads route through local Whisper before chat_brain sees them.
    # See tesseract/mirror/server/ws.py audio preprocessor.
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
}
_KIND_DIR = {
    "image": "image",
    "pdf": "pdf",
    "file": "document",
    "audio": "audio",
}


def _safe_filename(name: str) -> str:
    stem = Path(name).name.strip().replace(" ", "_")
    cleaned = _SAFE_NAME_RE.sub("_", stem).strip("._")
    return cleaned or "attachment"


def _safe_segment(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > _SAFE_SEGMENT_MAX:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return ""
    return value


def _detect_mime(filename: str, header_value: str) -> str | None:
    header = (header_value or "").split(";", 1)[0].strip().lower()
    ext_mime = _EXT_MIME.get(Path(filename).suffix.lower())
    if ext_mime is None:
        return None
    if header and header != "application/octet-stream" and header != ext_mime:
        return None
    if header and header != "application/octet-stream":
        return header
    return ext_mime


def _mime_matches_magic(mime_type: str, head: bytes) -> bool:
    if mime_type == "application/pdf":
        return head.startswith(b"%PDF-")
    if mime_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if mime_type == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if mime_type == "image/webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if mime_type == "audio/wav":
        # RIFF...WAVE
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if mime_type == "audio/mpeg":
        # ID3 tag or MPEG audio sync (frame header starts 0xFF 0xEx/0xFx)
        if head.startswith(b"ID3"):
            return True
        return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
    if mime_type in ("audio/mp4", "audio/x-m4a"):
        # ISO BMFF: ftyp box at offset 4
        return len(head) >= 8 and head[4:8] == b"ftyp"
    if mime_type == "audio/ogg":
        return head.startswith(b"OggS")
    if mime_type == "audio/flac":
        return head.startswith(b"fLaC")
    if mime_type == "audio/webm":
        # EBML magic — same container family as video/webm
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _kind_for_mime(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type == "application/pdf":
        return "pdf"
    return "file"
