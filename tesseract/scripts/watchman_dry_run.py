"""Run a whole watchman pass over the real logs and print every stage, writing nothing.

    python -m tesseract.scripts.watchman_dry_run [--hours 48] [--model qwen2.5:7b]

What it prints, in the order the runtime does it:

    1 collect   every finding the sources produced, before anything judged it
    2..5 judge  each verdict, which stage decided, and the tier it landed in
    part 1      the opening sentence, written by the template and by a model
    part 2      the incident lines the message carries
    part 3      the pointer
    part 4      the full record, which reaches the panel and disk only

**It writes nothing, and that is not a convenience.** `judge.judge` ends in
`suppress.apply` which ends in `standing.save`, so a pass run for a person to
look at would stamp every current fault as already announced and the next real
tick would suppress all of them. The save is neutered here, and the run asserts
afterwards that the store on disk is byte-identical to what it was before.

The model is optional. Without `--model` the template is the only part 1 shown,
which is the point of the comparison: the runtime has to be readable with no
model at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from tesseract.orchestrator.watchman import grouping, judge, probes, report, sources
from tesseract.orchestrator.watchman.findings import Finding, Sweep
from tesseract.orchestrator.watchman.judge import standing

# A Windows console defaults to cp1252 and the marks this prints are not in
# it. Reconfiguring is the whole fix: the strings are correct, the terminal
# was not being told what it was being handed.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RULE = "=" * 78


def _hr(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def _severity_mark(finding: Finding) -> str:
    return {"bad": "BAD ", "warn": "WARN", "info": "info"}[finding.severity]


# ── 1. collect ──────────────────────────────────────────────────────


def collect(*, hours: int, now: datetime) -> Sweep:
    import asyncio

    start = now - timedelta(hours=hours)
    swept = sources.sweep(window_start=start, window_end=now)
    diagnosed = asyncio.run(probes.read_diagnostics(now))
    return Sweep(
        window_start=swept.window_start,
        window_end=swept.window_end,
        reads=swept.reads + (diagnosed,),
    )


def show_collected(swept: Sweep) -> None:
    _hr("1 · COLLECT — what the sources wrote, before anything judged it")
    for read in swept.reads:
        if read.error:
            state = f"could not be read: {read.error}"
        elif not read.present:
            state = "not on this machine"
        elif read.findings:
            state = f"{len(read.findings)} finding(s)"
        else:
            state = "quiet"
        print(f"  {read.name:16} {read.scanned:>6} rows   {state}")
    print(f"\n  {len(swept.findings)} findings in total\n")
    for finding in swept.findings:
        when = finding.last_at.isoformat(timespec="seconds") if finding.last_at else "-"
        print(f"  [{_severity_mark(finding)}] {finding.source:16} {when}")
        print(f"         {finding.summary[:110]}")
        if finding.cause:
            print(f"         cause: {finding.cause[:100]}")


# ── 2..5 the judge ──────────────────────────────────────────────────


def show_judged(judged) -> None:  # noqa: ANN001
    _hr("2..5 · JUDGE — every verdict, and which stage decided it")
    for verdict in judged.verdicts:
        state = "KEPT   " if verdict.kept else "DROPPED"
        tier = verdict.tier or "-"
        print(f"  {state} {verdict.stage:10} {tier:14} "
              f"[{_severity_mark(verdict.finding)}] "
              f"{verdict.finding.summary[:70]}")
        print(f"          why: {verdict.why[:104]}")
    print(f"\n  collected {len(judged.sweep.findings)} · "
          f"kept {len(judged.kept)} · dropped {len(judged.dropped)} · "
          f"needs you {len(judged.needs_you)} · "
          f"worth knowing {len(judged.worth_knowing)}")
    if judged.blind_spots:
        print("\n  what the judge could not see:")
        for note in judged.blind_spots:
            print(f"    - {note}")


# ── part 1, both ways ───────────────────────────────────────────────


def ask_model(prompt: str, model: str) -> tuple[str, float]:
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2},
    }).encode("utf-8")
    request = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=body, headers={"Content-Type": "application/json"},
    )
    began = time.monotonic()
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                answer = json.load(response).get("response", "")
            return answer.strip(), time.monotonic() - began
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"    (retrying: {exc})", file=sys.stderr)
            time.sleep(5)
    return "", time.monotonic() - began


def show_part_one(urgent: tuple[Finding, ...], *, model: str) -> str:
    from tesseract.scheduler.tasks.watchman import build_prompt

    _hr("PART 1 · the opening sentence · goes to the channel")
    template = report.template_narration(urgent)
    print(f"  TEMPLATE ({len(template)} chars, no model, no download)\n")
    print(f"    {template or '(nothing needs you, so there is no sentence)'}")
    if not model or not urgent:
        return template

    facts = report.fact_lines(urgent)
    budget = report.narration_budget(facts)
    print(f"\n  MODEL · {model} · 3 samples · budget {budget} chars")
    kept = 0
    for index in range(1, 4):
        answer, seconds = ask_model(build_prompt(facts), model)
        faithful = report.is_faithful(answer, facts)
        kept += faithful
        print(f"\n  sample {index} — {len(answer)} chars, {seconds:.1f}s, "
              f"{'KEPT' if faithful else 'DROPPED by the faithfulness check'}")
        for line in (answer or "(empty)").split("\n"):
            if line.strip():
                print(f"    {line.strip()[:104]}")
    print(f"\n  {kept} of 3 samples would have reached the operator.")
    return template


# ── parts 2, 3, 4 ───────────────────────────────────────────────────


def show_the_message(urgent: tuple[Finding, ...], narration: str, path: Path) -> None:
    from tesseract.integrations.telegram.render import render_telegram
from tesseract.orchestrator.autonomy.message import compose

    _hr("PARTS 2 and 3 · the incidents and the pointer · go to the channel")
    if not urgent:
        print("  Nothing needs you, so no message is sent at all.")
        return
    body = render_telegram(compose("runtime_report", {
        "defects": len(urgent)),
        "severity": report.worst(urgent),
        "narration": narration,
        "lines": [f.summary for f in urgent],
        "report_path": str(path),
    })
    print(f"  {len(body)} characters, as Telegram receives it:\n")
    for line in body.split("\n"):
        print(f"    {line}")


def show_the_record(swept: Sweep, judged, narration: str, out: Path) -> Path:  # noqa: ANN001
    _hr("PART 4 · the full record · reaches the panel and disk only")
    text = report.render_summary(swept, narration=narration, judgement=judged)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    for line in text.split("\n"):
        print(f"  {line}")
    return out


# ── the run ─────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--model", default="", help="an Ollama model, or empty for none")
    parser.add_argument(
        "--out",
        default="",
        help="where to write part 4; defaults to a temp file beside this run",
    )
    args = parser.parse_args()

    # NOTHING IS WRITTEN. `suppress.apply` ends in `standing.save`, and a pass
    # run to be looked at would stamp every current fault as already announced.
    before = standing.store_path()
    fingerprint = (
        sha256(before.read_bytes()).hexdigest() if before.exists() else "absent"
    )
    saves = []
    standing.save = lambda entries, *, now: saves.append(len(entries)) or before  # type: ignore[assignment]

    now = datetime.now(timezone.utc)
    print(f"window: {args.hours}h up to {now.isoformat(timespec='seconds')}")

    swept = collect(hours=args.hours, now=now)
    show_collected(swept)

    judged = judge.judge(swept, now=now)
    show_judged(judged)

    urgent = grouping.group(judged.needs_you)
    narration = show_part_one(urgent, model=args.model)
    out = Path(args.out) if args.out else Path("watchman-dry-run.md")
    show_the_message(urgent, narration, out)
    show_the_record(judged.judged, judged, narration, out)

    after = (
        sha256(before.read_bytes()).hexdigest() if before.exists() else "absent"
    )
    _hr("WHAT THIS RUN WROTE")
    print(f"  standing store: {'UNCHANGED' if after == fingerprint else 'CHANGED — BUG'}")
    print(f"  saves intercepted: {len(saves)}")
    print(f"  part 4 written to: {out}")
    return 0 if after == fingerprint else 1


if __name__ == "__main__":
    raise SystemExit(main())
