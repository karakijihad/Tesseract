"""FTSIndex._sanitize_query must not raise FTS5 syntax errors on punctuation.

Bug: the old sanitizer stripped a denylist of special characters then
joined tokens with " OR ", which still let apostrophes, hyphens,
ampersands, and bare FTS5 keywords (AND/OR/NOT/NEAR) through as
unquoted operators — raising sqlite3.OperationalError inside
FTSIndex.search (caught -> "FTS search failed" -> empty results).

Fix: quote every whitespace token as an FTS5 string literal so it can
never be parsed as an operator.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.memory.fts_index import FTSIndex


def _index(tmp_path: Path) -> FTSIndex:
    return FTSIndex(db_path=tmp_path / "fts.db")


def test_sanitize_quotes_each_token() -> None:
    assert FTSIndex._sanitize_query("What's on your plate?") == (
        '"What\'s" OR "on" OR "your" OR "plate?"'
    )


def test_sanitize_empty_input_returns_empty_string() -> None:
    assert FTSIndex._sanitize_query("") == ""
    assert FTSIndex._sanitize_query("   ") == ""


PROBLEM_QUERIES = [
    "What's on your plate right now?",
    "foo-bar baz",
    "a & b",
    "plate AND now",
    "don't stop",
    'say "hi" there',
    "NEAR(x)",
]


def test_search_does_not_raise_on_problem_queries(tmp_path: Path) -> None:
    fts = _index(tmp_path)
    try:
        for query in PROBLEM_QUERIES:
            results = fts.search(query)
            assert results == []
    finally:
        fts.close()


def test_search_whitespace_only_returns_empty_without_running_query(tmp_path: Path) -> None:
    fts = _index(tmp_path)
    try:
        assert fts.search("   ") == []
    finally:
        fts.close()


def test_positive_match_still_works(tmp_path: Path) -> None:
    fts = _index(tmp_path)
    try:
        fts.add("mem-1", "Tesseract plate", "What's on your plate right now?")
        results = fts.search("plate")
        assert any(mem_id == "mem-1" for mem_id, _score in results)

        results = fts.search("What's on your plate right now?")
        assert any(mem_id == "mem-1" for mem_id, _score in results)
    finally:
        fts.close()
