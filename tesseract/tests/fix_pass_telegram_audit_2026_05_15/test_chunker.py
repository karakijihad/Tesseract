"""Audit fix M2 — Telegram reply chunker tests.

Pins the contract used by ``TelegramBridge._send_outbound`` and
``send_text``: paragraph-preferred splits, no chunk over the cap
(including the ``(n/N)`` suffix), and code-fence balancing so a long
answer with a fenced block never ships as half-open Telegram HTML.
"""

from __future__ import annotations

from tesseract.integrations.telegram.chunker import chunk_for_telegram


def test_short_body_returns_single_chunk_without_suffix() -> None:
    out = chunk_for_telegram("hello world")
    assert out == ["hello world"]


def test_empty_body_returns_empty_list() -> None:
    assert chunk_for_telegram("") == []
    assert chunk_for_telegram("   \n\n  ") == []


def test_chunks_at_paragraph_boundary() -> None:
    # Two paragraphs, each ~3000 chars — one paragraph fits per chunk.
    para_a = "a" * 3000
    para_b = "b" * 3000
    body = para_a + "\n\n" + para_b
    chunks = chunk_for_telegram(body, max_len=4000)
    assert len(chunks) == 2
    assert chunks[0].startswith(para_a)
    assert chunks[1].startswith(para_b)
    # Suffix tags present.
    assert "(1/2)" in chunks[0]
    assert "(2/2)" in chunks[1]


def test_all_chunks_respect_cap() -> None:
    body = ("paragraph one. " * 200 + "\n\n" + "paragraph two. " * 200) * 4
    for chunk in chunk_for_telegram(body, max_len=2000):
        assert len(chunk) <= 2000, f"chunk over cap: {len(chunk)}"


def test_long_paragraph_falls_back_to_line_split() -> None:
    line = "x" * 500
    body = "\n".join([line] * 10)  # 10 lines of 500 chars + newlines ~ 5100
    chunks = chunk_for_telegram(body, max_len=2000)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 2000


def test_oversize_single_word_hard_slices() -> None:
    body = "z" * 5000  # one word, no spaces
    chunks = chunk_for_telegram(body, max_len=1000)
    assert len(chunks) >= 5
    for chunk in chunks:
        assert len(chunk) <= 1000


def test_code_fence_balanced_across_chunks() -> None:
    fence_body = "```\n" + ("line\n" * 1500) + "```"  # ~7.5k chars inside fence
    chunks = chunk_for_telegram(fence_body, max_len=4000)
    assert len(chunks) >= 2
    for chunk in chunks:
        # Each chunk must have an even number of ``` markers (balanced).
        assert chunk.count("```") % 2 == 0, f"unbalanced fence in chunk: {chunk!r}"


def test_continuation_marker_format() -> None:
    body = "p1" + ("\n\np2" * 50) + ("\n\np3" * 50)
    chunks = chunk_for_telegram(body, max_len=200)
    n = len(chunks)
    assert n >= 2
    for idx, chunk in enumerate(chunks, start=1):
        assert f"({idx}/{n})" in chunk
