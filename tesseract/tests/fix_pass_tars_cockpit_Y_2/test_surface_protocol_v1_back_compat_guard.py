"""v1 schema guard — locks the wire contract. A version bump must be a
deliberate edit here, not an accident."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tesseract.orchestrator.surfaces.descriptor import (
    SCHEMA_VERSION,
    SurfaceDescriptor,
)


def _kwargs(**over):
    base = dict(
        id="x",
        type="folder",
        view="tars",
        position={"x": 0, "y": 0},
        size={"w": 1, "h": 1},
        created_at_utc="2026-06-03T00:00:00+00:00",
        updated_at_utc="2026-06-03T00:00:00+00:00",
    )
    base.update(over)
    return base


def test_version_is_one():
    assert SCHEMA_VERSION == 1, (
        "Surface Protocol is v1. A bump is a breaking change requiring a v2 "
        "descriptor + renderer back-compat (GOVERNANCE.md Rule 7) — update "
        "this guard intentionally."
    )


def test_v2_descriptor_rejected_with_clear_error():
    with pytest.raises(ValidationError) as exc:
        SurfaceDescriptor(**_kwargs(schema_version=2))
    msg = str(exc.value)
    assert "schema_version" in msg
    assert "v1" in msg


def test_additive_unknown_field_ignored_not_rejected():
    # Forward-additive: a file written by a newer build (extra optional key)
    # still loads. ``extra="ignore"`` on the descriptor.
    d = SurfaceDescriptor.model_validate(_kwargs(future_field={"k": "v"}))
    assert not hasattr(d, "future_field")
    assert d.schema_version == 1
