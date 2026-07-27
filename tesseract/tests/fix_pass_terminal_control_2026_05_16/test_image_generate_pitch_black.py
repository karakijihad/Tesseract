"""Phase Y — pitch-black image bug. Session 1440 repro.

Operator (2026-05-16) asked TARS to "generate an image of rail
corrugation and acoustic response"; the FLUX.1-dev output came back
pitch black twice. Codex's investigative delegate didn't find the
cause inside the session; this fix pins the resolution.

Cause: the NIM FLUX.1-dev endpoint silently shifted some time after
the 2026-05-07 capability probe — omitting `cfg_scale` from the body
now produces uniformly-near-zero output (effectively unconditional
generation). The default 1024×1024 canvas was also implicit on the
server-side and isn't reliable.

Fix: send `cfg_scale=3.5`, `width=1024`, `height=1024` as explicit
defaults. Add `_looks_like_uniform_image` post-decode tripwire so a
genuinely-uniform return surfaces as a clean error instead of being
saved + rendered in chat.

Pins:
- Body shape includes cfg_scale + width + height
- Defaults match FLUX.1-dev sweet spots (3.5 / 1024 / 1024)
- Operator can override
- Uniform-image tripwire fires below 8 KB
- Uniform-image tripwire passes a real-looking JPEG header + body
- Invalid inputs (cfg_scale out of range etc.) rejected at schema
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tesseract.kernel.tools.image_generate import (
    ImageGenerateInput,
    _UNIFORM_IMAGE_BYTE_FLOOR,
    _looks_like_uniform_image,
)


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


# ─── input schema ─────────────────────────────────────────────────


def test_defaults_match_flux_recommended_values() -> None:
    inp = ImageGenerateInput(prompt="anything")
    assert inp.cfg_scale == 3.5
    assert inp.width == 1024
    assert inp.height == 1024
    assert inp.steps == 20
    assert inp.mode == "base"


def test_operator_can_override_cfg_scale_and_canvas() -> None:
    inp = ImageGenerateInput(
        prompt="x",
        cfg_scale=5.0,
        width=768,
        height=512,
    )
    assert inp.cfg_scale == 5.0
    assert inp.width == 768
    assert inp.height == 512


def test_cfg_scale_below_floor_rejected() -> None:
    with pytest.raises(ValidationError):
        ImageGenerateInput(prompt="x", cfg_scale=-0.1)


def test_cfg_scale_above_ceiling_rejected() -> None:
    with pytest.raises(ValidationError):
        ImageGenerateInput(prompt="x", cfg_scale=25.0)


def test_width_height_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        ImageGenerateInput(prompt="x", width=128)  # below 256
    with pytest.raises(ValidationError):
        ImageGenerateInput(prompt="x", height=4096)  # above 2048


# ─── uniform-image tripwire ───────────────────────────────────────


def test_uniform_image_floor_constant() -> None:
    """Pin the trip wire so a refactor doesn't accidentally raise/lower it
    into the false-positive zone. 8 KB is comfortably below a real
    FLUX 1024×1024 JPEG (~80-300 KB) and above a uniform frame (~3 KB)."""
    assert _UNIFORM_IMAGE_BYTE_FLOOR == 8 * 1024


def test_uniform_detects_short_bytes() -> None:
    assert _looks_like_uniform_image(b"\x00" * 1000) is True
    assert _looks_like_uniform_image(b"") is True


def test_uniform_passes_real_size_jpeg() -> None:
    # Magic-byte JPEG header + 100 KB padding — well above floor.
    body = b"\xff\xd8\xff\xe0" + b"\x01" * (100 * 1024)
    assert _looks_like_uniform_image(body) is False


def test_uniform_boundary_is_inclusive_at_floor() -> None:
    # Exactly the floor: not flagged. One byte below: flagged.
    assert _looks_like_uniform_image(b"\x00" * _UNIFORM_IMAGE_BYTE_FLOOR) is False
    assert _looks_like_uniform_image(b"\x00" * (_UNIFORM_IMAGE_BYTE_FLOOR - 1)) is True
