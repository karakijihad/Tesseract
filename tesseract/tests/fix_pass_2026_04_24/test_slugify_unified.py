"""M7 regression — `_slugify` must be one function, not three.

Pre-fix: `vault_librarian._slugify` NFKD-normalized + ASCII-encoded input
(so `"résumé" → "resume"`), while `vault_lint._slugify_term` and
`vault_query._slugify_simple` did only lowercase + regex-sub (so
`"résumé" → "r-sum-"`). Lookup paths therefore fabricated missing-hub
findings for any accented entity ingested through `compile_source`.
"""

from __future__ import annotations

from tesseract.kernel.tools.vault_query import _slugify_simple
from tesseract.memory.vault_lint import _slugify_term
from tesseract.memory.vault_librarian import _slugify as _librarian_slugify
from tesseract.memory.vault_manager import slugify


def test_all_vault_slugifiers_agree_on_ascii() -> None:
    assert slugify("Temporal Decay") == "temporal-decay"
    assert _slugify_term("Temporal Decay") == "temporal-decay"
    assert _slugify_simple("Temporal Decay") == "temporal-decay"
    assert _librarian_slugify("Temporal Decay") == "temporal-decay"


def test_all_vault_slugifiers_agree_on_accented() -> None:
    term = "Résumé"
    expected = "resume"
    assert slugify(term) == expected
    assert _slugify_term(term) == expected
    assert _slugify_simple(term) == expected
    assert _librarian_slugify(term) == expected


def test_slugify_caps_length() -> None:
    assert len(slugify("a" * 200)) == 60
    assert len(_slugify_term("b" * 200)) == 60
    assert len(_slugify_simple("c" * 200)) == 60
