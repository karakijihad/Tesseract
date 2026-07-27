from __future__ import annotations

import os
import time

from tesseract.integrations.telegram.api import TelegramAPI, TelegramAPIError
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult


class TelegramNotifyJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail="TELEGRAM_BOT_TOKEN missing",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        raw_chat_id = (
            ctx.config.get("chat_id") or os.environ.get("TELEGRAM_DEFAULT_CHAT_ID") or ""
        )
        try:
            chat_id = int(str(raw_chat_id).strip())
        except ValueError:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail="chat_id missing or invalid",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        text = str(ctx.config.get("text") or "").strip()
        if not text:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail="text missing",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        parse_mode = ctx.config.get("parse_mode")
        parse_mode = str(parse_mode) if isinstance(parse_mode, str) and parse_mode else None
        api = TelegramAPI(token)
        try:
            result = await api.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except TelegramAPIError as exc:
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=str(exc),
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        finally:
            await api.aclose()
        return JobResult(
            job_name=ctx.job_name,
            run_id=ctx.run_id,
            ok=True,
            detail="telegram message sent",
            payload={"chat_id": chat_id, "message_id": result.get("message_id")},
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
