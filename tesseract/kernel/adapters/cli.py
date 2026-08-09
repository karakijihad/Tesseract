"""CLIAdapter — subprocess-backed chat_brain for ``codex`` (and later ``claude``).

The delegate tools (``delegate_auditor``, ``delegate_coder``) already spawn
these CLIs as one-shot subprocesses for tool calls. This adapter takes the
same subprocess machinery and wires it into the ``ModelAdapter.stream()``
contract so a CLI can serve as the primary chat_brain — every turn streams
through the operator's CLI subscription instead of an API key.

Scope
-----
* **Codex only for now.** Claude has a different stream-json schema; that's
  a follow-up. The dispatch in ``boot.build_adapter`` handles ``adapter='cli'``
  for both providers but only ``command == 'codex'`` actually parses
  events. ``claude`` falls back to plain-text mode.
* **No tool-call passthrough.** When codex internally invokes a command
  (file read, web search, MCP tool), we surface it as ``StreamChunk(TEXT)``
  for visibility; we do NOT route it back through the assistant's tool registry.
  Codex has its own toolbox; bridging the two is stage-3 work.
* **Heuristic token counting.** Codex doesn't expose a tokenizer; we
  approximate by character count. Cost ledger attribution still lands
  per ``turn.completed.usage``.

Codex `exec --json` event schema (verified via live capture):

    {"type": "thread.started", "thread_id": "..."}
    {"type": "turn.started"}
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "..."}}
    {"type": "turn.completed", "usage": {"input_tokens": N, "cached_input_tokens": N,
                                         "output_tokens": N, "reasoning_output_tokens": N}}
    {"type": "error", "message": "..."}

Codex does NOT emit per-token deltas — each completed message arrives as
one `item.completed` event with the full text. The chat UI receives a
single `StreamChunk(TEXT, text=full)` per agent message instead of
incremental deltas. Live "typing" effect is sacrificed in exchange for
zero adapter-side buffering complexity.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, AsyncGenerator

from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ErrorKind,
    ModelAdapter,
    StreamChunk,
)
from tesseract.kernel.adapters.cli_utils import (
    claude_subscription_env,
    codex_subscription_env,
    resolve_codex_executable,
)
from tesseract.kernel.adapters.errors import classify_exception

log = logging.getLogger(__name__)

_NON_TEXT_ITEM_TYPES = frozenset(
    {"command_execution", "mcp_tool_call", "web_search", "file_change", "plan_update"}
)

_HARD_ERROR_NEEDLES = (
    "unauthorized",
    "forbidden",
    "invalid api key",
    "authentication",
    "auth required",
    "quota exceeded",
    "insufficient quota",
    "billing",
    "context length",
    "model not found",
)
_TRANSIENT_ERROR_NEEDLES = (
    "rate limit",
    "rate-limit",
    "too many requests",
    "overloaded",
    "temporarily unavailable",
    "server error",
    "timeout",
    "timed out",
    "connection reset",
)


def _classify_cli_error(message: str) -> ErrorKind:
    lowered = (message or "").lower()
    if any(needle in lowered for needle in _HARD_ERROR_NEEDLES):
        return ErrorKind.HARD
    if any(needle in lowered for needle in _TRANSIENT_ERROR_NEEDLES):
        return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten a chat-history `messages` list into a single prompt string.

    Codex `exec` accepts one task argument; it doesn't have a multi-turn
    chat history concept on the CLI surface. We render the history as a
    role-prefixed transcript so the model sees prior context. System role
    becomes a leading directive block; user/assistant alternate.
    """
    parts: list[str] = []
    system_blocks: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        text = _coerce_content(content)
        if not text:
            continue
        if role == "system":
            system_blocks.append(text)
        elif role == "user":
            parts.append(f"USER: {text}")
        elif role == "assistant":
            parts.append(f"ASSISTANT: {text}")
        elif role == "tool":
            parts.append(f"TOOL_RESULT: {text}")
    header = "\n\n".join(system_blocks).strip()
    body = "\n\n".join(parts).strip()
    return f"{header}\n\n---\n\n{body}".strip() if header else body


def _coerce_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if "text" in block:
                    chunks.append(str(block["text"]))
                elif block.get("type") == "text":
                    chunks.append(str(block.get("text", "")))
        return "\n".join(c for c in chunks if c)
    return ""


_DATA_URL_PREFIX = "data:"


def _extract_image_blocks(messages: list[dict[str, Any]]) -> list[tuple[bytes, str]]:
    """Walk every content block and return raw image bytes + media_type.

    Recognises the three shapes that land in the assistant today:
      * OpenAI / Responses: ``{"type": "image_url", "image_url": {"url": "data:..."}}``
        or ``{"type": "input_image", "image_url": "data:..."}``
      * Anthropic: ``{"type": "image", "source": {"type": "base64",
        "media_type": "image/png", "data": "<base64>"}}``
      * Direct ``data:image/<ext>;base64,<...>`` URLs in either of the above.

    Non-data URLs (https://) are skipped — codex only takes local files.
    """
    out: list[tuple[bytes, str]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("image_url", "input_image"):
                url_field = block.get("image_url")
                url = url_field.get("url") if isinstance(url_field, dict) else url_field
                pair = _decode_data_url(url) if isinstance(url, str) else None
                if pair is not None:
                    out.append(pair)
            elif btype == "image":
                source = block.get("source")
                if isinstance(source, dict) and source.get("type") == "base64":
                    media = str(source.get("media_type") or "image/png")
                    data = source.get("data")
                    if isinstance(data, str):
                        try:
                            out.append((base64.b64decode(data), media))
                        except Exception:
                            log.warning("CLIAdapter: skipped malformed base64 image block")
    return out


def _decode_data_url(url: str) -> tuple[bytes, str] | None:
    if not url.startswith(_DATA_URL_PREFIX):
        return None
    try:
        head, _, payload = url[len(_DATA_URL_PREFIX):].partition(",")
        if ";base64" in head:
            media = head.split(";", 1)[0] or "image/png"
            return base64.b64decode(payload), media
        media = head.split(";", 1)[0] or "image/png"
        return urllib.parse.unquote_to_bytes(payload), media
    except Exception:
        log.warning("CLIAdapter: skipped malformed image data URL")
        return None


def _media_extension(media_type: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }
    return mapping.get(media_type.lower(), ".bin")


class CLIAdapter(ModelAdapter):
    """Streaming adapter backed by a subprocess CLI (codex / claude)."""

    def __init__(
        self,
        *,
        command: str,
        model_id: str,
        timeout: float,
        stream_json: bool = True,
    ) -> None:
        self.command = command
        self.model_id = model_id
        self.timeout = timeout
        self.stream_json = stream_json

    def _resolve_executable(self) -> str:
        if self.command == "codex":
            return resolve_codex_executable()
        return shutil.which(self.command) or self.command

    def _build_env(self) -> dict[str, str]:
        if self.command == "codex":
            return codex_subscription_env()
        if self.command == "claude":
            return claude_subscription_env()
        import os
        return os.environ.copy()

    def _build_argv(self, image_paths: list[Path] | None = None) -> tuple[str, ...]:
        """Argv WITHOUT the prompt — prompt is fed via stdin to dodge
        Windows' ~32 KB argv cap (WinError 206). System prompt + chat
        history routinely exceeds that limit.

        - `codex exec [-i FILE]... --json -` reads the prompt from stdin.
          Each `-i FILE` attaches a local image to the initial prompt.
        - `claude -p` with no argv prompt reads from stdin. claude CLI's
          `--file file_id:path` form requires Anthropic Files API IDs
          (which need an API key), so local image attachment is not
          wired through claude — see `cli.claude.opus_47.capabilities`.
        """
        executable = self._resolve_executable()
        image_args: tuple[str, ...] = ()
        if self.command == "codex" and image_paths:
            image_args = tuple(arg for path in image_paths for arg in ("-i", str(path)))
        if self.command == "codex" and self.stream_json:
            return (executable, "exec", *image_args, "--json", "-")
        if self.command == "codex":
            return (executable, "exec", *image_args, "-")
        if self.command == "claude" and self.stream_json:
            return (executable, "-p", "--output-format", "stream-json")
        if self.command == "claude":
            return (executable, "-p", "--output-format", "text")
        return (executable, "exec", "-")

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        if tools:
            log.warning(
                "CLIAdapter (%s) is chat_brain but %d tool definitions were dropped — "
                "this CLI cannot call the assistant tools. Memory, vault, and delegation will not work "
                "until tool-call passthrough is implemented.",
                self.command,
                len(tools),
            )
            yield StreamChunk(
                type=ChunkType.TEXT,
                text=(
                    f"[note: chat_brain is `{self.command}` CLI; the assistant tools "
                    "(memory_save, vault_search, delegate_*) are unavailable this turn]\n"
                ),
            )

        prompt = _flatten_messages(messages)
        # Image attachment: only wired for codex (`-i FILE`). claude CLI's
        # --file form requires Anthropic Files API IDs (API-key only), so
        # claude image passthrough is intentionally not implemented here.
        image_paths: list[Path] = []
        image_dir: tempfile.TemporaryDirectory | None = None
        if self.command == "codex":
            images = _extract_image_blocks(messages)
            if images:
                image_dir = tempfile.TemporaryDirectory(prefix="agent_cli_imgs_")
                base = Path(image_dir.name)
                for idx, (data, media) in enumerate(images):
                    path = base / f"img_{idx:02d}{_media_extension(media)}"
                    path.write_bytes(data)
                    image_paths.append(path)
                log.info(
                    "CLIAdapter (codex): attaching %d image(s) via -i", len(image_paths)
                )

        argv = self._build_argv(image_paths if image_paths else None)
        env = self._build_env()

        # Verify the executable exists before spawning so we can label the
        # error correctly. asyncio's CreateProcess can raise FileNotFoundError
        # for several distinct reasons on Windows (binary missing, path too
        # long, permission denied) and a single except branch labels them all
        # as "CLI not found", which has misled operators in the past.
        if shutil.which(argv[0]) is None:
            if image_dir is not None:
                image_dir.cleanup()
            yield StreamChunk(
                type=ChunkType.ERROR,
                error=f"{self.command} CLI not found on PATH (looked for {argv[0]!r})",
                error_kind=ErrorKind.HARD,
            )
            return

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            yield StreamChunk(
                type=ChunkType.ERROR,
                error=f"{self.command} failed to start: {exc}",
                error_kind=classify_exception(exc),
            )
            return

        # Feed the prompt over stdin, then close. Argv length cap on Windows
        # (~32 KB) is the original motivation; stdin has no such limit.
        assert process.stdin is not None
        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            log.warning("%s stdin write failed: %s", self.command, exc)
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

        assert process.stdout is not None
        assert process.stderr is not None
        usage: dict[str, Any] = {}
        had_error = False
        deadline = asyncio.get_event_loop().time() + self.timeout
        # Drain stderr concurrently so the OS pipe buffer can't fill and stall
        # codex's stdout writes (Windows anonymous pipes default to ~4 KB).
        stderr_task = asyncio.create_task(process.stderr.read())

        try:
            if self.command == "codex" and self.stream_json:
                async for chunk in self._pump_codex_json(process, deadline):
                    if chunk.type == ChunkType.ERROR:
                        had_error = True
                    if chunk.type == ChunkType.STOP and chunk.raw:
                        usage = dict(chunk.raw.get("usage") or {})
                    yield chunk
            else:
                async for chunk in self._pump_plain_text(process, deadline):
                    if chunk.type == ChunkType.ERROR:
                        had_error = True
                    yield chunk
        except asyncio.TimeoutError:
            await self._kill(process)
            yield StreamChunk(
                type=ChunkType.ERROR,
                error=f"{self.command} stream timed out after {self.timeout}s",
                error_kind=ErrorKind.TRANSIENT,
            )
            return
        except asyncio.CancelledError:
            await self._kill(process)
            raise
        finally:
            stderr_task.cancel()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                await self._kill(process)
            try:
                stderr_bytes = await stderr_task
            except (asyncio.CancelledError, Exception):
                stderr_bytes = b""
            if image_dir is not None:
                try:
                    image_dir.cleanup()
                except Exception:
                    log.warning("CLIAdapter: failed to clean image temp dir %s", image_dir.name)

        rc = process.returncode or 0
        if rc != 0 and not had_error:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            error_msg = f"{self.command} exited with {rc}: {stderr_text[:500]}"
            yield StreamChunk(
                type=ChunkType.ERROR,
                error=error_msg,
                error_kind=_classify_cli_error(stderr_text),
            )
            return
        if not had_error and not usage:
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end", raw={"usage": {}})

    async def _pump_codex_json(
        self, process: asyncio.subprocess.Process, deadline: float
    ) -> AsyncGenerator[StreamChunk, None]:
        assert process.stdout is not None
        loop = asyncio.get_event_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=remaining
            )
            if not line:
                return
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            async for chunk in self._map_codex_event(event):
                yield chunk

    async def _map_codex_event(
        self, event: dict[str, Any]
    ) -> AsyncGenerator[StreamChunk, None]:
        etype = str(event.get("type") or "")
        if etype in {"thread.started", "turn.started"}:
            return
        if etype == "item.completed":
            item = event.get("item") or {}
            item_type = str(item.get("type") or "")
            text = item.get("text") or ""
            if item_type == "agent_message" and isinstance(text, str) and text:
                yield StreamChunk(type=ChunkType.TEXT, text=text)
                return
            if item_type in _NON_TEXT_ITEM_TYPES and text:
                tag = item_type.replace("_", " ")
                yield StreamChunk(type=ChunkType.TEXT, text=f"\n[{tag}] {text}")
            return
        if etype == "turn.completed":
            usage = event.get("usage") or {}
            yield StreamChunk(
                type=ChunkType.STOP,
                stop_reason="end",
                raw={"usage": usage},
            )
            return
        if etype in {"turn.failed", "error"}:
            msg = str(event.get("message") or event.get("error") or "codex error")
            yield StreamChunk(
                type=ChunkType.ERROR,
                error=msg,
                error_kind=_classify_cli_error(msg),
            )
            return

    async def _pump_plain_text(
        self, process: asyncio.subprocess.Process, deadline: float
    ) -> AsyncGenerator[StreamChunk, None]:
        assert process.stdout is not None
        loop = asyncio.get_event_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        stdout = await asyncio.wait_for(process.stdout.read(), timeout=remaining)
        text = stdout.decode("utf-8", errors="replace").strip()
        if text:
            yield StreamChunk(type=ChunkType.TEXT, text=text)
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end", raw={"usage": {}})

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("%s subprocess did not exit within 5s after kill", self.command)

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        total += len(str(block["text"])) // 4
        return total

    async def check_available(self) -> bool:
        return shutil.which(self.command) is not None or shutil.which(
            f"{self.command}.cmd"
        ) is not None
