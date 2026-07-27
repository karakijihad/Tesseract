"""Smoke test — does Gemini 2.5 Flash-Lite reply?

Reads GOOGLE_API_KEY from tesseract/.env, sends a short prompt,
streams the response to stdout, prints timing.

Hits the live Google API. Not collected by pytest (filename has no
`test_` prefix). Run directly: `python -m tesseract.tests.smoke.smoke_gemini`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

MODEL = "gemini-2.5-flash-lite"
PROMPT = "Reply in one sentence: what are you?"


def main() -> int:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(env_path)

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print(f"[fail] GOOGLE_API_KEY not found in {env_path}", file=sys.stderr)
        return 1

    client = genai.Client(api_key=api_key)

    print(f"[model] {MODEL}")
    print(f"[prompt] {PROMPT}\n[reply] ", end="", flush=True)

    t0 = time.monotonic()
    first_token_at: float | None = None
    total_chars = 0

    stream = client.models.generate_content_stream(
        model=MODEL,
        contents=PROMPT,
        config=types.GenerateContentConfig(
            temperature=0.7,
            max_output_tokens=256,
        ),
    )

    for chunk in stream:
        text = chunk.text or ""
        if text and first_token_at is None:
            first_token_at = time.monotonic()
        sys.stdout.write(text)
        sys.stdout.flush()
        total_chars += len(text)

    elapsed = time.monotonic() - t0
    ttft_ms = int((first_token_at - t0) * 1000) if first_token_at else -1
    print(
        f"\n\n[timing] TTFT={ttft_ms}ms · total={int(elapsed * 1000)}ms · chars={total_chars}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
