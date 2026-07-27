"""Document attachment handler — text extraction for the common kinds (CR-2B)."""

from __future__ import annotations

import asyncio
import logging
from io import BytesIO

log = logging.getLogger(__name__)


_PLAIN_TEXT_MIMES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "application/json",
        "application/x-ndjson",
        "text/csv",
        "text/tab-separated-values",
        "text/tsv",
        "application/yaml",
        "text/yaml",
    }
)

_PLAIN_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {".txt", ".md", ".markdown", ".json", ".ndjson", ".csv", ".tsv", ".yaml", ".yml"}
)

_PDF_MIMES: frozenset[str] = frozenset({"application/pdf", "application/x-pdf"})


class DocumentHandlerError(RuntimeError):
    """Raised when extraction fails. Bridge maps to ``extract_failed``."""


def _normalize_mime(mime: str | None) -> str:
    if not mime:
        return ""
    return mime.split(";", 1)[0].strip().lower()


def _suffix_of(filename: str | None) -> str:
    if not filename:
        return ""
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def _is_pdf(mime: str, suffix: str) -> bool:
    return mime in _PDF_MIMES or suffix == ".pdf"


def _is_plain_text(mime: str, suffix: str) -> bool:
    return mime in _PLAIN_TEXT_MIMES or suffix in _PLAIN_TEXT_SUFFIXES


def _truncate(text: str, *, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Latin-1 always succeeds; documents are typically UTF-8 today,
        # but a 90s-vintage .txt forwarded over Telegram still deserves
        # a best-effort decode rather than a stack trace.
        return data.decode("latin-1", errors="replace")


def _extract_pdf_sync(data: bytes, *, max_chars: int) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentHandlerError("pypdf not installed") from exc

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise DocumentHandlerError(f"PDF open failed: {exc}") from exc

    parts: list[str] = []
    char_count = 0
    for idx, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            log.warning("document handler: page %d extract failed: %s", idx + 1, exc)
            continue
        if not text.strip():
            continue
        # Page separators help TARS reason about layout when several
        # pages worth of content fit under the cap. Trim them on the
        # final assembly to avoid a leading separator.
        marker = f"\n\n--- page {idx + 1} ---\n\n"
        budget = max_chars - char_count - len(marker)
        if max_chars > 0 and budget <= 0:
            parts.append("\n[truncated — extract_chars cap reached]")
            break
        chunk = text if max_chars <= 0 else text[:budget]
        parts.append(marker + chunk)
        char_count += len(marker) + len(chunk)

    body = "".join(parts).strip()
    if not body:
        raise DocumentHandlerError(
            "scanned PDF — no extractable text"
        )
    return _truncate(body, max_chars=max_chars)


async def extract_document_text(
    data: bytes,
    *,
    mime: str | None = None,
    filename: str | None = None,
    max_chars: int = 6000,
) -> str:
    """Return extracted text for ``data``; routes on MIME then filename suffix."""
    if not data:
        return ""

    normalized = _normalize_mime(mime)
    suffix = _suffix_of(filename)

    if _is_pdf(normalized, suffix):
        return await asyncio.to_thread(_extract_pdf_sync, data, max_chars=max_chars)

    if _is_plain_text(normalized, suffix):
        text = _decode_text(data)
        cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not cleaned:
            raise DocumentHandlerError("document body was empty after decode")
        return _truncate(cleaned, max_chars=max_chars)

    descriptor = mime or suffix or "unknown"
    raise DocumentHandlerError(f"no extractor for document mime/suffix={descriptor}")


__all__ = ["DocumentHandlerError", "extract_document_text"]
