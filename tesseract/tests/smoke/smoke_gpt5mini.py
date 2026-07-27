"""One-shot GPT-5 mini smoke test via the brain config + adapter builders.

Hits the live OpenAI API — requires `OPENAI_API_KEY` in `tesseract/.env`.
Not collected by pytest (filename has no `test_` prefix). Run directly:
`python -m tesseract.tests.smoke.smoke_gpt5mini`.
"""
from __future__ import annotations

import asyncio
import sys
import time

from dotenv import load_dotenv

from tesseract.brain.boot import (
    ENV_PATH,
    build_chat_brain_adapter,
    load_chat_brain_config,
)
from tesseract.brain.chat import ChatSession
from tesseract.brain.prompt import assemble_system_prompt
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType


async def main() -> int:
    load_dotenv(ENV_PATH)
    cfg = load_chat_brain_config()
    print(f"primary: {cfg.provider}/{cfg.model}")
    adapter = build_chat_brain_adapter(cfg)
    options = AdapterOptions(
        model=cfg.model,
        provider=cfg.provider,
        temperature=cfg.temperature,
        max_output_tokens=cfg.max_output_tokens,
        context_window=cfg.context_window,
        reasoning_effort=cfg.reasoning_effort,
        use_responses_api=cfg.use_responses_api,
    )
    session = ChatSession(
        adapter=adapter,
        system_prompt=assemble_system_prompt(),
        max_tool_iterations=cfg.tool_iteration_cap,
        max_consecutive_adapter_errors=cfg.consecutive_error_cap,
        options=options,
    )

    t0 = time.monotonic()
    ttft = None
    chars = 0
    async for ch in session.send("Who are you?"):
        if ch.type == ChunkType.TEXT:
            if ttft is None:
                ttft = time.monotonic()
            sys.stdout.write(ch.text)
            sys.stdout.flush()
            chars += len(ch.text)
        elif ch.type == ChunkType.ERROR:
            print(f"\n[ERROR] {ch.error}", file=sys.stderr)
            return 1
    total = time.monotonic() - t0
    ttft_ms = int((ttft - t0) * 1000) if ttft else -1
    print(f"\n[TTFT={ttft_ms}ms total={int(total * 1000)}ms chars={chars}]")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
