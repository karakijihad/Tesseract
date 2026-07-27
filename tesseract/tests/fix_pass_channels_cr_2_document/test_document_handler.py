"""CR-2B unit tests: :mod:`tesseract.integrations._handlers.document`.

PDF parsing is delegated to ``pypdf`` — we monkeypatch its ``PdfReader``
to avoid coupling tests to a real PDF binary. The plain-text and
mime-dispatch logic is exercised directly because it's pure decode.
"""

from __future__ import annotations

import pytest

from tesseract.integrations._handlers.document import (
    DocumentHandlerError,
    extract_document_text,
)


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, pages: list[str]) -> None:
        self.pages = [_FakePage(t) for t in pages]


def _patch_pdf_reader(monkeypatch, reader_or_exc) -> None:
    def _factory(buf):
        del buf
        if isinstance(reader_or_exc, BaseException):
            raise reader_or_exc
        return reader_or_exc

    monkeypatch.setattr("pypdf.PdfReader", _factory)


@pytest.mark.asyncio
async def test_empty_data_returns_empty_string() -> None:
    out = await extract_document_text(b"", mime="application/pdf")
    assert out == ""


@pytest.mark.asyncio
async def test_plain_text_utf8_decode_strips_and_normalizes_newlines() -> None:
    out = await extract_document_text(
        b"  hello\r\nworld\r\n  ", mime="text/plain"
    )
    assert out == "hello\nworld"


@pytest.mark.asyncio
async def test_plain_text_latin1_fallback() -> None:
    payload = "café\n".encode("latin-1")  # 0xE9 — invalid UTF-8 single byte
    out = await extract_document_text(payload, mime="text/plain")
    assert out.endswith("\xe9")


@pytest.mark.asyncio
async def test_markdown_via_mime() -> None:
    out = await extract_document_text(
        b"# Title\n\nBody.", mime="text/markdown"
    )
    assert out == "# Title\n\nBody."


@pytest.mark.asyncio
async def test_plain_text_via_filename_suffix_when_mime_missing() -> None:
    out = await extract_document_text(
        b"line1\nline2", mime=None, filename="notes.md"
    )
    assert out == "line1\nline2"


@pytest.mark.asyncio
async def test_plain_text_truncates_at_max_chars_with_ellipsis() -> None:
    out = await extract_document_text(
        b"x" * 500, mime="text/plain", max_chars=20
    )
    assert len(out) == 20
    assert out.endswith("…")


@pytest.mark.asyncio
async def test_plain_text_empty_after_decode_raises() -> None:
    with pytest.raises(DocumentHandlerError) as exc_info:
        await extract_document_text(b"   \n\n", mime="text/plain")
    assert "empty" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_pdf_extracts_text_across_pages(monkeypatch) -> None:
    _patch_pdf_reader(
        monkeypatch,
        _FakeReader(["Page one body.", "", "Third page text."]),
    )
    out = await extract_document_text(b"%PDF-1.4 fake", mime="application/pdf")
    assert "Page one body." in out
    assert "Third page text." in out
    assert "--- page 1 ---" in out
    assert "--- page 3 ---" in out
    # Empty page 2 contributes nothing to the body and gets no marker.
    assert "--- page 2 ---" not in out


@pytest.mark.asyncio
async def test_pdf_truncates_at_max_chars_with_marker(monkeypatch) -> None:
    _patch_pdf_reader(
        monkeypatch,
        _FakeReader(["A" * 200, "B" * 200, "C" * 200]),
    )
    out = await extract_document_text(
        b"%PDF-1.4 fake", mime="application/pdf", max_chars=100
    )
    # ``_truncate`` may rstrip a trailing whitespace before the ellipsis,
    # so the post-truncate length can be max_chars-1 or max_chars.
    assert 99 <= len(out) <= 100
    assert out.endswith("…")


@pytest.mark.asyncio
async def test_pdf_scanned_no_text_raises_specific_error(monkeypatch) -> None:
    _patch_pdf_reader(monkeypatch, _FakeReader(["", "  ", ""]))
    with pytest.raises(DocumentHandlerError) as exc_info:
        await extract_document_text(b"%PDF-1.4 fake", mime="application/pdf")
    assert "scanned" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_pdf_open_failure_wrapped(monkeypatch) -> None:
    _patch_pdf_reader(monkeypatch, RuntimeError("corrupt header"))
    with pytest.raises(DocumentHandlerError) as exc_info:
        await extract_document_text(b"%PDF-broken", mime="application/pdf")
    assert "PDF open failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unsupported_mime_raises_with_descriptor() -> None:
    with pytest.raises(DocumentHandlerError) as exc_info:
        await extract_document_text(
            b"binary",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="report.docx",
        )
    msg = str(exc_info.value)
    assert "no extractor" in msg
    # Should name something specific so TARS can apologize precisely.
    assert "docx" in msg.lower() or "wordprocessing" in msg.lower()


@pytest.mark.asyncio
async def test_pdf_dispatch_via_filename_when_mime_generic() -> None:
    captured: list[bytes] = []

    class _GoodReader:
        pages = [_FakePage("from pdf")]

    def _factory(buf):
        # buf is a BytesIO of the fake bytes — capture the raw bytes for
        # assertion that bridge data reaches the extractor unchanged.
        captured.append(buf.getvalue())
        return _GoodReader()

    # Patch via monkeypatch fixture mechanism replacement.
    import pypdf  # noqa: PLC0415

    original = pypdf.PdfReader
    pypdf.PdfReader = _factory  # type: ignore[assignment]
    try:
        out = await extract_document_text(
            b"%PDF-1.4 payload",
            mime="application/octet-stream",
            filename="anything.pdf",
        )
    finally:
        pypdf.PdfReader = original  # type: ignore[assignment]
    assert "from pdf" in out
    assert captured == [b"%PDF-1.4 payload"]
