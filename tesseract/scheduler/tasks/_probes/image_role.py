"""Image-generation role probe — text-to-image known-good call.

Triggers ``image_generate`` with a high-contrast prompt and a pinned
square frame, so a drifting endpoint is the only thing that can change
the result shape. Healthy responses return a JPEG/PNG/WebP of at least
the ``_UNIFORM_IMAGE_BYTE_FLOOR`` size; smaller payloads come back from
the tool as ``is_error=True`` with the uniform-image message, which the
probe maps to ``drift_kind="uniform_output"``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, ClassVar

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.image_generate import (
    ImageGenerateInput,
    ImageGenerateTool,
)
from tesseract.scheduler.tasks._probes.base import ProbeResult

log = logging.getLogger(__name__)

_KNOWN_GOOD_PROMPT = "a single red apple on a white table, photograph"


class ImageRoleProbe:
    role_kind: ClassVar[str] = "image_generation"

    def __init__(self, tool: ImageGenerateTool | None = None) -> None:
        # Stateless tool; tests can inject a fake exposing ``run``.
        self._tool = tool or ImageGenerateTool()

    async def probe(self, role_name: str, ref: str) -> ProbeResult:
        ctx = ToolContext(
            workspace_root="",
            session_id=f"provider-probe-{role_name}",
            current_call_id=f"provider-probe-{role_name}",
        )
        inp = ImageGenerateInput(
            prompt=_KNOWN_GOOD_PROMPT,
            model_role=role_name,
            aspect_ratio="1:1",
        )
        return await _run_probe(self._tool, inp, ctx, role_name, ref)


async def _run_probe(
    tool: Any,
    inp: ImageGenerateInput,
    ctx: ToolContext,
    role_name: str,
    ref: str,
) -> ProbeResult:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    try:
        result = await tool.run(inp, ctx)
    except Exception as exc:  # noqa: BLE001
        log.warning("image probe crashed for role=%s: %r", role_name, exc)
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="http_error",
            evidence={"exception": repr(exc)},
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    latency_ms = (time.monotonic() - t0) * 1000.0
    metadata = getattr(result, "metadata", {}) or {}
    output = getattr(result, "output", "") or ""
    if getattr(result, "is_error", False):
        drift = _classify_image_error(output, metadata)
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind=drift,
            evidence={"output": output[:500], "metadata": dict(metadata)},
            probed_at=now,
            latency_ms=latency_ms,
        )
    return ProbeResult(
        role=role_name,
        ref=ref,
        ok=True,
        drift_kind="none",
        evidence={
            "output": output[:200],
            "size_bytes": metadata.get("size_bytes"),
            "mime_type": metadata.get("mime_type"),
        },
        probed_at=now,
        latency_ms=latency_ms,
    )


def _classify_image_error(output: str, metadata: dict[str, Any]) -> str:
    """Map the tool's free-form error message to a ``DriftKind``.

    The tool's uniform-image branch produces a stable substring ("uniform")
    and carries ``image_bytes`` in metadata; everything else is bucketed as
    ``http_error`` or ``unavailable`` based on the message shape.
    """
    text = output.lower()
    if "uniform" in text or "image_bytes" in metadata:
        return "uniform_output"
    if "unavailable" in text or "disabled" in text or "missing" in text:
        return "unavailable"
    if "non-json" in text or "shape not recognized" in text or "invalid base64" in text:
        return "shape_mismatch"
    return "http_error"


__all__ = ["ImageRoleProbe"]
