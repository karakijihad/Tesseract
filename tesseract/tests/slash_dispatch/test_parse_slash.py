"""Coverage for ``tesseract.scripts.slash_dispatch.parse_slash``."""

from __future__ import annotations

import pytest

from tesseract.scripts.slash_dispatch import parse_slash


def test_returns_none_without_leading_slash():
    assert parse_slash("hello") is None
    assert parse_slash("") is None


def test_returns_none_when_only_slash():
    assert parse_slash("/") is None
    assert parse_slash("/   ") is None


def test_simple_name_only():
    name, kv, positional = parse_slash("/help")
    assert name == "help"
    assert kv == {}
    assert positional == []


def test_kv_pairs():
    name, kv, positional = parse_slash("/alarm_set label=standup when=10m")
    assert name == "alarm_set"
    assert kv == {"label": "standup", "when": "10m"}
    assert positional == []


def test_quoted_value():
    name, kv, positional = parse_slash('/alarm_set label=ping message="team sync"')
    assert kv["message"] == "team sync"


def test_positional_token_collected():
    name, kv, positional = parse_slash("/memory_search how to foo")
    assert name == "memory_search"
    assert kv == {}
    assert positional == ["how", "to", "foo"]


def test_quoted_positional_stays_one_token():
    name, kv, positional = parse_slash('/memory_search "how to foo"')
    assert positional == ["how to foo"]


def test_mixed_kv_and_positional():
    # Both are returned; coerce_args will reject the mix.
    name, kv, positional = parse_slash("/alarm_set label=x bare when=10m")
    assert kv == {"label": "x", "when": "10m"}
    assert positional == ["bare"]


def test_mismatched_quote_raises():
    with pytest.raises(ValueError, match="mismatched"):
        parse_slash('/alarm_set message="unterminated')


def test_equals_with_empty_key_treated_as_positional():
    # "=value" has no key — fall through to positional rather than swallow silently.
    name, kv, positional = parse_slash("/x =foo")
    assert kv == {}
    assert positional == ["=foo"]
