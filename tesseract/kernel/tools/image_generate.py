"""ImageGenerateTool — text-to-image and image-to-image via the configured
`image_generator` role.

Walks the role's primary + fallbacks, skipping any catalog entry whose
`capabilities` don't cover the requested operation (text_to_image /
image_to_image). For the first capable, reachable provider it posts the
request, decodes the returned image (base64 or a fetched URL), persists it
under the downloads tree via `mirror.server.downloads.save_download`, and
returns the public `/api/downloads/...` URL so the chat surface can render
it inline. Failures on one provider advance to the next; only an exhausted
chain returns ``ToolResult(is_error=True)`` — never a crash.

Per-model request contract is selected by `payload_profile`, which every
`kind: image_generation` catalog entry must declare — there is no default,
because guessing a body shape produces a 400 that reads like an outage.

* ``gemini_images`` — Google's Interactions API (`POST /v1beta/interactions`).
    Authenticates with the `x-goog-api-key` header, NOT a bearer token.
    Body: {"model","input":[{"type":"text","text": ...}
           (+ {"type":"image","mime_type","data": <b64>} for image-to-image)],
           "response_format"?: {"type":"image","aspect_ratio": ...}}
    Response: {"steps":[{"type":"thought","signature": <huge b64>},
               {"type":"model_output","content":[{"type":"image","data": <b64>}]}]}
    The thought step's `signature` is a large base64 blob that is NOT an
    image; extraction keys on `content[].type == "image"` to avoid it.
* ``xai_images`` — xAI Grok Imagine (OpenAI-images-style). Text-to-image hits
    `.../v1/images/generations`; image-to-image hits `.../v1/images/edits`
    (source image passed as a base64 data URI in `image`). Body:
    {"model","prompt","image?"}.  Response:
    {"data":[{"b64_json"|"url": ...}]} — URL responses are fetched to bytes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import uuid
from typing import Any, ClassVar, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from tesseract import http_client
from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)

logger = logging.getLogger(__name__)


class ImageGenerateInput(BaseModel):
    prompt: str = Field(
        description="Description of the image to generate (or the edit to apply, for image-to-image). Be concrete and visual.",
        min_length=1,
    )
    model_role: str = Field(
        default="image_generator",
        description=(
            "Role name in roles.yaml that picks the image-gen catalog entry. "
            "Default `image_generator` resolves to the operator-configured "
            "primary + fallbacks."
        ),
    )
    aspect_ratio: Optional[
        Literal[
            "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
            "9:16", "16:9", "21:9", "1:8", "8:1", "1:4", "4:1",
        ]
    ] = Field(
        default=None,
        description=(
            "Shape of the generated image (gemini_images profile). Omit to let "
            "the model choose one that suits the subject — it does this well, so "
            "only set it when the frame is part of the request (a square avatar, "
            "a 16:9 header). Ignored by the xai_images profile."
        ),
    )
    image_attachment_id: Optional[str] = Field(
        default=None,
        description=(
            "Source image for image-to-image editing. When set, the tool runs "
            "image-to-image and only uses providers whose catalog `capabilities` "
            "declare `image_to_image: true` (e.g. grok-imagine-image); "
            "text-to-image-only providers are skipped in the fallback chain."
        ),
    )


class ImageGenerateTool(Tool):
    # Operator-driven (chat_brain only fires it when the user asks for an
    # image). `auto` keeps testing-phase friction off; flip to `ask` in
    # `permissions.yaml` once cost-aware providers are the norm (grok imagine
    # is paid, if cheap).
    default_posture = "auto"

    # Network egress + writes a file to downloads/ — `propose` per the
    # AU-3 reviewer (image artifact is a user-visible state change).
    risk_class: ClassVar[str] = "propose"
    group: ClassVar[str] = "showing-the-operator"
    summary: ClassVar[str] = (
        "Generate an image from a text prompt, or edit a source image (image-to-image)."
    )
    use_when: ClassVar[str] = (
        "The operator asks you to draw, illustrate, render, edit, or restyle "
        "a visual. Pass `image_attachment_id` for image-to-image. Saves the "
        "result under downloads and returns its URL."
    )
    not_when: ClassVar[str] = (
        "`open` to show an image that already exists — this tool MAKES one, "
        "it doesn't display one."
    )

    @property
    def name(self) -> str:
        return "image_generate"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ImageGenerateInput

    def is_concurrency_safe(self) -> bool:
        return True  # Pure HTTP POST — no shared state.

    def is_read_only(self) -> bool:
        return False  # Writes an image to disk.

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, ImageGenerateInput)
            else ImageGenerateInput(**tool_input.model_dump())
        )

        # Resolve the role -> catalog chain. Failure here means the role is
        # missing, the catalog entry is gone, or the YAML is malformed.
        try:
            from tesseract.config.loader import load_config
            bundle = load_config()
            role = bundle.role(inp.model_role)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                output=f"{inp.model_role} role unavailable: {exc}",
                is_error=True,
            )

        if role.mode != "active" or role.primary is None:
            return _emit_unavailable(
                context, inp.model_role,
                f"{inp.model_role} role is inactive in roles.yaml — wire a "
                f"provider and set `mode: active` to enable",
            )

        # Operation = image-to-image when a source image is supplied, else
        # text-to-image. The chain is filtered by each model's declared
        # `capabilities` so an entry that can't serve the op is skipped.
        op = "image_to_image" if inp.image_attachment_id else "text_to_image"

        session_id = context.session_id if context is not None else ""

        source_image_b64: str | None = None
        if op == "image_to_image":
            source_image_b64 = await _resolve_uploaded_image(
                inp.image_attachment_id or "", session_id,
            )
            if source_image_b64 is None:
                return ToolResult(
                    output=(
                        f"image-to-image requested but source attachment "
                        f"{inp.image_attachment_id!r} could not be loaded"
                    ),
                    is_error=True,
                )

        # `skip_reason` records why entries were passed over (disabled / no
        # capability); `attempt_error` holds the last REAL provider failure.
        # The final message prefers `attempt_error` so an i2i failure surfaces
        # what actually went wrong on Grok, not a downstream capability skip.
        chain = [role.primary, *role.fallbacks]
        skip_reason = "no image_generator providers configured"
        attempt_error: str | None = None
        for ref in chain:
            conn = ref.connection
            if not conn.tier_enabled or not conn.enabled:
                skip_reason = f"{ref.ref}: provider disabled in providers.yaml"
                continue
            caps = ref.model.fields.get("capabilities") or {}
            if not bool(caps.get(op, False)):
                skip_reason = f"{ref.ref}: no `{op}` capability"
                continue
            result = await self._attempt(ref, inp, op, source_image_b64, context)
            if result is not None and not result.is_error:
                return result
            if result is not None:
                attempt_error = result.output
            # advance to the next chain entry on any error

        return _emit_unavailable(
            context, inp.model_role,
            f"{inp.model_role}: no provider could serve {op} "
            f"({attempt_error or skip_reason})",
        )

    async def _attempt(
        self,
        ref: Any,
        inp: ImageGenerateInput,
        op: str,
        source_image_b64: str | None,
        context: ToolContext,
    ) -> ToolResult | None:
        """One provider attempt. Returns a success ToolResult, or an
        is_error ToolResult (the caller advances to the next chain entry)."""
        conn = ref.connection

        base_url = ref.model.fields.get("base_url_override")
        if not isinstance(base_url, str) or not base_url:
            return ToolResult(
                output=f"{ref.ref}: catalog entry has no `base_url_override`",
                is_error=True,
            )

        api_key_env = conn.api_key_env or ""
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            return ToolResult(
                output=f"{ref.ref}: env var {api_key_env!r} not set",
                is_error=True,
            )

        payload_profile = ref.model.fields.get("payload_profile")
        if not isinstance(payload_profile, str) or not payload_profile:
            return ToolResult(
                output=(
                    f"{ref.ref}: catalog entry has no `payload_profile` — every "
                    f"`kind: image_generation` entry must name the request "
                    f"contract it speaks (see providers.yaml)"
                ),
                is_error=True,
            )

        await _emit_status(
            context, f"calling image_generator ({ref.model.model}, {op})…",
        )

        try:
            url, body = _build_request(
                base_url, ref.model.model, inp, op, payload_profile,
                source_image_b64,
            )
        except _ProfileError as exc:
            return ToolResult(output=f"{ref.ref}: {exc}", is_error=True)

        timeout = float(conn.timeout_seconds)
        try:
            async with http_client.async_client(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    headers=_auth_headers(payload_profile, api_key),
                    json=body,
                )
        except httpx.HTTPError as exc:
            _note_image_tripwire(
                inp.model_role, ref.ref, "http_error", {"exception": repr(exc)},
            )
            return ToolResult(output=f"{ref.ref} HTTP error: {exc}", is_error=True)

        if resp.status_code != 200:
            _note_image_tripwire(
                inp.model_role, ref.ref, "http_error",
                {"status_code": resp.status_code, "body": resp.text[:200]},
            )
            return ToolResult(
                output=f"{ref.ref} returned HTTP {resp.status_code}: {resp.text[:500]}",
                is_error=True,
            )

        try:
            payload = resp.json()
        except ValueError:
            _note_image_tripwire(
                inp.model_role, ref.ref, "shape_mismatch",
                {"reason": "non-JSON response", "body": resp.text[:200]},
            )
            return ToolResult(output=f"{ref.ref} returned non-JSON response", is_error=True)

        image_bytes = await _bytes_from_payload(payload, timeout)
        if image_bytes is None:
            _note_image_tripwire(
                inp.model_role, ref.ref, "shape_mismatch",
                {"reason": "no image in known response shapes",
                 "keys": list(payload.keys()) if isinstance(payload, dict) else []},
            )
            return ToolResult(
                output=(
                    f"{ref.ref} response shape not recognized (expected "
                    "`artifacts[0].base64`, `data[0].b64_json`, or `data[0].url`)"
                ),
                is_error=True,
            )

        # 2026-05-16 sanity check: a near-uniform (all-black / single-color)
        # frame compresses catastrophically — a healthy 1024×1024 image runs
        # ~80-300 KB; a uniform frame collapses below ~10 KB.
        if _looks_like_uniform_image(image_bytes):
            _note_image_tripwire(
                inp.model_role, ref.ref, "uniform_output",
                {"image_bytes": len(image_bytes),
                 "aspect_ratio": inp.aspect_ratio,
                 "prompt": inp.prompt[:200]},
            )
            hint = "Re-check the endpoint contract for this model."
            return ToolResult(
                output=(
                    f"{ref.ref} returned what looks like a uniform "
                    f"(black / single-color) frame ({len(image_bytes)} bytes — "
                    f"far below the ~80 KB floor for a real 1024×1024 image). {hint}"
                ),
                is_error=True,
                metadata={
                    "image_bytes": len(image_bytes),
                    "aspect_ratio": inp.aspect_ratio,
                    "prompt": inp.prompt[:200],
                },
            )

        suffix, mime_type = _suffix_and_mime(image_bytes)

        # Persist under downloads/ mirroring the uploads layout so the chat
        # surface can fetch via the public URL the helper returns.
        from tesseract.mirror.server.downloads import save_download
        session_id = (context.session_id if context is not None else "") or "anon"
        filename = f"img_{uuid.uuid4().hex[:12]}{suffix}"
        try:
            rec = await save_download(
                session_id=session_id,
                filename=filename,
                data=image_bytes,
                mime_type=mime_type,
                source_tool=self.name,
            )
        except Exception as exc:  # noqa: BLE001 — disk full / permission / etc.
            return ToolResult(
                output=f"{ref.ref} failed to persist artifact: {exc}",
                is_error=True,
            )

        return ToolResult(
            output=rec.url,
            metadata={
                "role": inp.model_role,
                "model": ref.model.model,
                "operation": op,
                "artifact_id": rec.id,
                "filename": rec.filename,
                "mime_type": rec.mime_type,
                "kind": rec.kind,
                "size_bytes": rec.size,
                "storage_path": rec.storage_path,
                "aspect_ratio": inp.aspect_ratio,
            },
        )


class _ProfileError(Exception):
    """Raised by `_build_request` when no request can be built — the catalog
    entry names a `payload_profile` this tool does not implement. Surfaced as
    an is_error ToolResult so the chain advances to the next provider."""


def _auth_headers(profile: str, api_key: str) -> dict[str, str]:
    """Auth is part of a profile's contract, not an operator choice — Google's
    Interactions API rejects a bearer token and wants its key in a header of
    its own, while the OpenAI-shaped surfaces want `Authorization`."""
    common = {"Accept": "application/json", "Content-Type": "application/json"}
    if profile == "gemini_images":
        return {"x-goog-api-key": api_key, **common}
    return {"Authorization": f"Bearer {api_key}", **common}


def _build_request(
    base_url: str,
    model: str,
    inp: ImageGenerateInput,
    op: str,
    profile: str,
    source_image_b64: str | None,
) -> tuple[str, dict[str, Any]]:
    """Return (url, json_body) for one provider attempt.

    gemini_images: one endpoint for both ops — image-to-image is just a
    second `input` block carrying the source image inline.

    xai_images: text-to-image → /v1/images/generations; image-to-image →
    /v1/images/edits (derived by swapping the path suffix), with the source
    image as a base64 data URI.
    """
    if profile == "gemini_images":
        parts: list[dict[str, Any]] = [{"type": "text", "text": inp.prompt}]
        if op == "image_to_image":
            # An attachment arrives as whatever the operator uploaded, so the
            # declared mime has to be sniffed rather than assumed — Google
            # validates this field and rejects the image outright when it
            # disagrees with the bytes.
            parts.append({
                "type": "image",
                "mime_type": _mime_from_b64(source_image_b64 or ""),
                "data": source_image_b64 or "",
            })
        body: dict[str, Any] = {"model": model, "input": parts}
        if inp.aspect_ratio is not None:
            body["response_format"] = {
                "type": "image",
                "aspect_ratio": inp.aspect_ratio,
            }
        return base_url, body

    if profile == "xai_images":
        # Minimal body matching xAI's documented examples ({model, prompt}
        # [+ image]). We deliberately DON'T send `n`/`response_format`/size —
        # unknown params risk a 400 on the primary, and `_bytes_from_payload`
        # already handles either a b64 or a URL response.
        if op == "image_to_image":
            # xAI edits endpoint. base_url_override points at .../generations;
            # swap the suffix so operators configure one URL.
            url = base_url.replace("/images/generations", "/images/edits")
            data_uri = f"data:image/png;base64,{source_image_b64 or ''}"
            body: dict[str, Any] = {
                "model": model,
                "prompt": inp.prompt,
                "image": {"image_url": data_uri},
            }
        else:
            url = base_url
            body = {
                "model": model,
                "prompt": inp.prompt,
            }
        return url, body

    raise _ProfileError(
        f"unknown payload_profile {profile!r} for {model} — the catalog entry "
        f"names a request contract this tool does not implement"
    )


async def _bytes_from_payload(payload: Any, timeout: float) -> bytes | None:
    """Decode image bytes from a response — base64 first, else fetch a URL.

    Covers the Gemini Interactions `steps[].content[]` shape, the OpenAI-images
    `data[0].b64_json` shape, and the xAI `data[0].url` shape (fetched to
    bytes). Returns None when no known shape carries an image.
    """
    if not isinstance(payload, dict):
        return None
    b64 = _extract_image_b64(payload)
    if b64:
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError):
            return None
    url = _extract_image_url(payload)
    if url:
        try:
            async with http_client.async_client(timeout=timeout) as client:
                r = await client.get(url)
                if r.status_code == 200 and r.content:
                    return r.content
        except httpx.HTTPError:
            return None
    return None


def _extract_image_b64(payload: dict[str, Any]) -> str:
    """Pull base64 image data out of the Gemini Interactions shape or the
    OpenAI-compatible /v1/images/generations shape."""
    # Gemini: walk the steps and take the block that SAYS it is an image.
    # A `thought` step sits alongside carrying a ~1 MB `signature` blob, so
    # "the longest base64 string in the response" is the wrong rule here.
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            for block in step.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "image":
                    continue
                b64 = block.get("data")
                if isinstance(b64, str) and b64:
                    return b64
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            b64 = first.get("b64_json") or first.get("base64")
            if isinstance(b64, str) and b64:
                return b64
    return ""


def _extract_image_url(payload: dict[str, Any]) -> str:
    """Pull a fetchable image URL out of the `data[0].url` shape (xAI)."""
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            u = first.get("url")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                return u
    return ""


def _suffix_and_mime(image_bytes: bytes) -> tuple[str, str]:
    """Pick file suffix + MIME from the magic bytes so the saved file matches
    what came down the wire (Gemini returns JPEG; others may differ)."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return ".jpg", "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png", "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".png", "image/png"


def _mime_from_b64(b64: str) -> str:
    """MIME of a base64 payload, read from its magic bytes. Only the header is
    decoded — an uploaded image can be several megabytes and the caller is
    about to send the encoded form anyway."""
    try:
        head = base64.b64decode(b64[:32], validate=False)
    except (ValueError, TypeError):
        return "image/png"
    return _suffix_and_mime(head)[1]


async def _emit_status(context: ToolContext, message: str) -> None:
    if context is None or context.status_emit is None:
        return
    try:
        await context.status_emit(message)
    except Exception:  # noqa: BLE001 — never let status emit fail the call
        logger.debug("image_generate: status_emit failed", exc_info=True)


def _emit_unavailable(context: ToolContext, role_name: str, message: str) -> ToolResult:
    """Send the operator a status line for a soft-fail and return is_error=True.

    Synchronous wrapper that schedules the status emit on the running loop.
    Used for early-return paths (inactive role, exhausted chain) where we
    want both the visible toast AND the structured ToolResult.
    """
    if context is not None and context.status_emit is not None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(context.status_emit(message))
        except RuntimeError:
            # No running loop (sync test context) — silent.
            pass
    return ToolResult(output=message, is_error=True)


def _note_image_tripwire(role: str, ref: str, drift_kind: str, evidence: dict) -> None:
    """AU-14 14b production tripwire. Wraps the JSONL telemetry write so the
    tool's error paths don't import the orchestrator module eagerly."""
    try:
        from tesseract.orchestrator.provider_health import note_production_tripwire
        note_production_tripwire(role, ref, drift_kind, evidence)
    except Exception:  # noqa: BLE001
        logger.debug("image_generate: tripwire write failed", exc_info=True)


# Empirical floor: a real 1024×1024 JPEG/PNG runs ~80-300 KB; a uniform frame
# (all-black / single color) drops below ~10 KB. 8 KB trip wire; false-positive
# risk is small. Adjust if a model with much smaller canvases is wired in.
_UNIFORM_IMAGE_BYTE_FLOOR = 8 * 1024


def _looks_like_uniform_image(image_bytes: bytes) -> bool:
    """Return True when `image_bytes` is suspiciously small for its container,
    indicating a uniform (likely black) frame. Heuristic only — uniformity
    collapses compressed output ~30× regardless of codec."""
    return len(image_bytes) < _UNIFORM_IMAGE_BYTE_FLOOR


async def _resolve_uploaded_image(
    attachment_id: str,
    session_id: str,
) -> str | None:
    """Read a previously-uploaded image attachment and return it base64.

    The bytes are passed through as stored, so callers that must declare a
    mime type read it off the magic bytes (`_mime_from_b64`) rather than
    assuming the upload was any particular format.
    """
    if not attachment_id:
        return None
    try:
        from tesseract.mirror.server.uploads import load_attachment
        from tesseract.mirror.server.uploads._storage import _attachment_file_path
    except ImportError:
        return None
    att = load_attachment(session_id, attachment_id)
    if att is None or att.kind != "image":
        return None
    path = _attachment_file_path(att)
    if path is None:
        return None
    raw = await asyncio.get_event_loop().run_in_executor(None, path.read_bytes)
    return base64.b64encode(raw).decode("ascii")
