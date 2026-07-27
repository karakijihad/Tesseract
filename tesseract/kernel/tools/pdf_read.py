"""PdfReadTool — extract text from a PDF by page range.

Read-only, local file. PASSTHROUGH permission — same risk profile as
FileReadTool, just a different decoder.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

_MAX_PAGES_PER_CALL = 50  # cap per invocation — pick page ranges for large PDFs
_MAX_CHARS_PER_CALL = 120_000


class PdfReadInput(BaseModel):
    file_path: str = Field(description="Path to the PDF (absolute or workspace-relative)")
    pages: str = Field(
        default="",
        description="Page range, e.g. '1-5', '3', '10-20'. Empty = read from page 1 up to the page cap.",
    )


class PdfReadTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "pdf_read"

    @property
    def description(self) -> str:
        return (
            "Extract text from a PDF file. Use `pages` (e.g. '1-5') for large PDFs. "
            f"Capped at {_MAX_PAGES_PER_CALL} pages / {_MAX_CHARS_PER_CALL:,} chars per call."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return PdfReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, PdfReadInput) else PdfReadInput(**tool_input.model_dump())
        path = Path(inp.file_path)
        if not path.is_absolute():
            path = Path(context.workspace_root) / path

        if not path.exists():
            return ToolResult(output=f"PDF not found: {path}", is_error=True)
        if not path.is_file():
            return ToolResult(output=f"Not a file: {path}", is_error=True)

        try:
            from pypdf import PdfReader
        except ImportError:
            return ToolResult(output="pypdf not installed", is_error=True)

        try:
            reader = PdfReader(str(path))
        except Exception as e:
            return ToolResult(output=f"Failed to open PDF: {e}", is_error=True)

        total_pages = len(reader.pages)
        start, end = _parse_pages(inp.pages, total_pages)
        if start is None:
            return ToolResult(output=f"Invalid page range: {inp.pages!r}", is_error=True)

        parts: list[str] = []
        char_count = 0
        for i in range(start, end):
            if context.cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                text = reader.pages[i].extract_text() or ""
            except Exception as e:
                parts.append(f"[page {i + 1}: extract failed — {e}]")
                continue
            marker = f"\n--- page {i + 1} ---\n"
            remaining = _MAX_CHARS_PER_CALL - char_count - len(marker)
            if remaining <= 0:
                parts.append(f"[truncated — char cap at page {i + 1}; request later pages]")
                break
            chunk = text[:remaining]
            parts.append(marker + chunk)
            char_count += len(marker) + len(chunk)

        header = f"PDF: {path.name} ({total_pages} pages total), showing pages {start + 1}-{end}"
        return ToolResult(
            output=header + "".join(parts),
            metadata={"total_pages": total_pages, "pages_read": end - start, "chars": char_count},
        )


def _parse_pages(spec: str, total: int) -> tuple[int | None, int]:
    """Return (start_0based, end_exclusive). (None, 0) on invalid spec."""
    cap = min(total, _MAX_PAGES_PER_CALL)
    if not spec.strip():
        return 0, cap

    s = spec.strip()
    try:
        if "-" in s:
            a, b = s.split("-", 1)
            start = max(0, int(a) - 1)
            end = min(total, int(b))
        else:
            start = max(0, int(s) - 1)
            end = min(total, start + 1)
    except ValueError:
        return None, 0

    if start >= end or start >= total:
        return None, 0
    end = min(end, start + _MAX_PAGES_PER_CALL)
    return start, end
