"""Coverage for ``tesseract.scripts.slash_dispatch.coerce_args``."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from tesseract.scripts.slash_dispatch import coerce_args


class _OneRequired(BaseModel):
    query: str


class _Multi(BaseModel):
    label: str
    count: int = 1
    enabled: bool = False


class _Optional(BaseModel):
    name: str = "default"


def test_kv_simple():
    out = coerce_args(_Multi, {"label": "x"}, [])
    assert out.label == "x"
    assert out.count == 1


def test_kv_int_coercion():
    out = coerce_args(_Multi, {"label": "x", "count": "42"}, [])
    assert out.count == 42


def test_kv_bool_coercion_true_words():
    for word in ("true", "TRUE", "yes", "y", "1", "on"):
        out = coerce_args(_Multi, {"label": "x", "enabled": word}, [])
        assert out.enabled is True


def test_kv_bool_coercion_false_words():
    for word in ("false", "no", "n", "0", "off"):
        out = coerce_args(_Multi, {"label": "x", "enabled": word}, [])
        assert out.enabled is False


def test_unknown_key_raises():
    with pytest.raises(ValueError, match="unknown args"):
        coerce_args(_Multi, {"label": "x", "ghost": "1"}, [])


def test_missing_required_raises():
    with pytest.raises(ValueError):
        coerce_args(_Multi, {}, [])


def test_positional_single_required_binds():
    out = coerce_args(_OneRequired, {}, ["how", "to", "foo"])
    assert out.query == "how to foo"


def test_positional_no_required_raises():
    # _Optional has no required fields — positional has nowhere to bind.
    with pytest.raises(ValueError, match="exactly one required field"):
        coerce_args(_Optional, {}, ["foo"])


def test_positional_multi_required_raises():
    # _Multi has one required (label); count and enabled have defaults.
    # That actually matches "exactly one required" — bind to label.
    out = coerce_args(_Multi, {}, ["just", "label"])
    assert out.label == "just label"


def test_mixing_positional_and_kv_rejected():
    with pytest.raises(ValueError, match="mixing"):
        coerce_args(_Multi, {"count": "2"}, ["label_value"])


class _ListField(BaseModel):
    tags: list[str] = Field(default_factory=list)


def test_json_list_coerced():
    out = coerce_args(_ListField, {"tags": '["a", "b"]'}, [])
    assert out.tags == ["a", "b"]
