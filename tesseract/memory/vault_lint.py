"""Vault lint — five-pass wiki auditor (orphan, stale, contradict, missing-hub, scale).

Lint is proposal, not action; `VaultLinter.run()` returns a `VaultLintReport`.
`dry_run=True` skips all filesystem writes.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import time as _time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING

from tesseract.agents.loader import AgentDefinition, load_agent
from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.vault_librarian import _parse_llm_json
from tesseract.memory.vault_manager import VaultManager

if TYPE_CHECKING:
    from tesseract.brain.boot import VaultConfig

logger = logging.getLogger(__name__)

_WRITABLE_VERDICTS = frozenset({"weaken", "qualify", "contradict"})
_MISSING_HUB_MIN_MENTIONS = 3
_MISSING_HUB_MAX_SUGGESTIONS = 10
_RESERVED_STEMS = frozenset({"INDEX", "TAXONOMY", "ingest-log", "LINT-REPORT"})
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class ContradictionFinding:
    slug_a: str
    slug_b: str
    verdict: str  # weaken | qualify | contradict
    reason: str


@dataclass(frozen=True)
class MissingHubFinding:
    term: str
    mention_count: int
    suggested_slug: str


@dataclass
class VaultLintReport:
    orphans: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    contradictions: list[ContradictionFinding] = field(default_factory=list)
    missing_hubs: list[MissingHubFinding] = field(default_factory=list)
    scale_alarm: bool = False
    scale_page_count: int = 0
    failures: list[str] = field(default_factory=list)


class VaultLinter:
    def __init__(
        self,
        vault_manager: VaultManager,
        config: "VaultConfig",
        adapter: ModelAdapter | None,
        adapter_options: AdapterOptions,
        log_dir: Path | None = None,
        agents_dir: Path | None = None,
    ) -> None:
        self._manager = vault_manager
        self._config = config
        self._adapter = adapter
        self._adapter_options = adapter_options
        self._breaker = CircuitBreaker(name="vault_lint", max_failures=3, log_dir=log_dir)
        self._agents_dir = agents_dir
        self._agent: AgentDefinition | None = None

    async def run(self, dry_run: bool = False) -> VaultLintReport:
        report = VaultLintReport()
        source_slugs = self._list_source_slugs()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        self._pass_orphan(source_slugs, report, today, dry_run)
        self._pass_stale(source_slugs, report, today, dry_run)
        await self._pass_contradict(source_slugs, report, today, dry_run)
        self._pass_missing_hub(source_slugs, report, today, dry_run)
        self._pass_scale(report, today, dry_run)
        return report

    # ── pass implementations ──

    def _pass_orphan(
        self,
        source_slugs: list[str],
        report: VaultLintReport,
        today: str,
        dry_run: bool,
    ) -> None:
        for slug in source_slugs:
            fm = self._manager.read_wiki_page_frontmatter(slug)
            if not fm:
                continue
            if not (fm.get("backlinks_from") or []) and not (fm.get("related_slugs") or []):
                report.orphans.append(slug)
                if not dry_run:
                    self._manager.update_lint_flags(
                        slug, [{"kind": "orphan", "detected": today}]
                    )

    def _pass_stale(
        self,
        source_slugs: list[str],
        report: VaultLintReport,
        today: str,
        dry_run: bool,
    ) -> None:
        grace = self._config.stale_grace_days
        for slug in source_slugs:
            fm = self._manager.read_wiki_page_frontmatter(slug)
            if not fm or not _is_stale(self._manager.root, fm, grace_days=grace):
                continue
            report.stale.append(slug)
            if not dry_run:
                self._manager.update_lint_flags(
                    slug, [{"kind": "stale", "detected": today}]
                )

    async def _pass_contradict(
        self,
        source_slugs: list[str],
        report: VaultLintReport,
        today: str,
        dry_run: bool,
    ) -> None:
        concepts_by_slug = self._concept_map(source_slugs)
        pairs = _build_contradiction_pairs(
            source_slugs, concepts_by_slug, cap=self._config.contradiction_pair_limit,
        )
        if not pairs:
            return

        adapter, options = self._get_adapter()
        if adapter is None:
            report.failures.append("contradict: no adapter available")
            return

        template = self._get_agent().get_section("Contradiction Prompt")
        if not template:
            report.failures.append("contradict: vault-lint.md missing 'Contradiction Prompt' section")
            return

        # Guard on failures *this run*, not the cross-run `is_tripped` flag.
        # The breaker rehydrates as tripped on a fresh process (so the
        # JSONL reflects last known state for the conscience signal), but
        # a new run should still attempt the first call — half-open probe.
        # Success heals (record_success writes the "reset" event because
        # is_tripped is still True at call time); 3 in-run failures stop
        # us hammering. Without this, a rehydrated-tripped breaker
        # short-circuits every run forever and never gets a chance to heal.
        failures_this_run = 0
        for slug_a, slug_b, shared in pairs:
            if failures_this_run >= self._breaker.max_failures:
                report.failures.append(f"contradict: breaker tripped at ({slug_a}, {slug_b})")
                break
            summary_a = _page_summary(self._manager, slug_a)
            summary_b = _page_summary(self._manager, slug_b)
            prompt = template.format(
                slug_a=slug_a,
                slug_b=slug_b,
                summary_a=summary_a,
                summary_b=summary_b,
                shared_concepts=", ".join(sorted(shared)),
            )
            try:
                raw = await adapter.generate(prompt, options)
                self._breaker.record_success()
            except Exception as exc:  # noqa: BLE001 — breaker records + continues
                self._breaker.record_failure(str(exc))
                failures_this_run += 1
                report.failures.append(f"contradict: adapter error on ({slug_a}, {slug_b}): {exc}")
                continue

            verdict, reason = _parse_verdict(raw)
            if verdict not in _WRITABLE_VERDICTS:
                continue

            finding = ContradictionFinding(
                slug_a=slug_a, slug_b=slug_b, verdict=verdict, reason=reason,
            )
            report.contradictions.append(finding)
            if dry_run:
                continue
            base_flag = {"kind": verdict, "reason": reason, "detected": today}
            self._manager.update_lint_flags(slug_a, [{**base_flag, "against": slug_b}])
            self._manager.update_lint_flags(slug_b, [{**base_flag, "against": slug_a}])

    def _pass_missing_hub(
        self,
        source_slugs: list[str],
        report: VaultLintReport,
        today: str,
        dry_run: bool,
    ) -> None:
        counts: Counter[str] = Counter()
        for slug in source_slugs:
            fm = self._manager.read_wiki_page_frontmatter(slug)
            if not fm:
                continue
            for term in list(fm.get("concepts") or []) + list(fm.get("entities") or []):
                if isinstance(term, str) and term.strip():
                    counts[term] += 1

        findings: list[MissingHubFinding] = []
        for term, n in counts.items():
            if n < _MISSING_HUB_MIN_MENTIONS:
                continue
            suggested = _slugify_term(term)
            if not suggested or self._manager.wiki_page_exists(suggested):
                continue
            findings.append(MissingHubFinding(
                term=term, mention_count=n, suggested_slug=suggested,
            ))
        findings.sort(key=lambda f: (-f.mention_count, f.term))
        findings = findings[:_MISSING_HUB_MAX_SUGGESTIONS]
        report.missing_hubs.extend(findings)

        if findings and not dry_run:
            self._append_lint_report(findings, today)

    def _pass_scale(
        self,
        report: VaultLintReport,
        today: str,
        dry_run: bool,
    ) -> None:
        wiki = self._manager.wiki_dir
        if not wiki.exists():
            return
        # audit-1 m4 (2026-04-24): reserved stems (INDEX, TAXONOMY,
        # ingest-log, LINT-REPORT) are vault bookkeeping, not Source pages.
        # Counting them toward scale_split_threshold fires the alarm ~3–4
        # pages earlier than the documented threshold implies.
        count = sum(1 for p in wiki.glob("*.md") if p.stem not in _RESERVED_STEMS)
        report.scale_page_count = count
        if count > self._config.scale_split_threshold:
            report.scale_alarm = True
            if not dry_run:
                self._manager.update_lint_flags(
                    "INDEX", [{"kind": "scale", "detected": today}]
                )

    # ── helpers ──

    def _list_source_slugs(self) -> list[str]:
        wiki = self._manager.wiki_dir
        if not wiki.exists():
            return []
        slugs: list[str] = []
        for path in sorted(wiki.glob("*.md")):
            if path.stem in _RESERVED_STEMS:
                continue
            fm = self._manager.read_wiki_page_frontmatter(path.stem)
            if fm.get("type") == "Source":
                slugs.append(path.stem)
        return slugs

    def _concept_map(self, slugs: list[str]) -> dict[str, set[str]]:
        """Shared concepts + entities per slug, lowercased for matching."""
        out: dict[str, set[str]] = {}
        for slug in slugs:
            fm = self._manager.read_wiki_page_frontmatter(slug)
            if not fm:
                out[slug] = set()
                continue
            terms: set[str] = set()
            for key in ("concepts", "entities"):
                for term in fm.get(key) or []:
                    if isinstance(term, str) and term.strip():
                        terms.add(term.strip().lower())
            out[slug] = terms
        return out

    def _append_lint_report(self, findings: list[MissingHubFinding], today: str) -> None:
        path = self._manager.wiki_dir / "LINT-REPORT.md"
        self._manager.wiki_dir.mkdir(parents=True, exist_ok=True)
        header = "# Vault Lint Report\n\n"
        # Lock around read-modify-write so a manual `vault_lint` invocation
        # overlapping a scheduled run can't silently drop the other's entries.
        with _exclusive_lock(path):
            body = ""
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                body = existing[len(header):] if existing.startswith(header) else existing
            entry_lines = [f"## {today} — missing-hub suggestions", ""]
            for f in findings:
                entry_lines.append(
                    f"- **{f.term}** (mentioned {f.mention_count}×) → suggested slug `{f.suggested_slug}`"
                )
            entry_lines.append("")
            new_content = header + "\n".join(entry_lines) + "\n" + body
            tmp = path.with_suffix(".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            tmp.replace(path)

    def _get_agent(self) -> AgentDefinition:
        if self._agent is None:
            self._agent = load_agent("vault-lint", agents_dir=self._agents_dir)
        return self._agent

    def _get_adapter(self) -> tuple[ModelAdapter | None, AdapterOptions | None]:
        if self._adapter is None:
            return None, None
        agent = self._get_agent()
        options = self._adapter_options
        if agent.max_tokens_override is not None:
            options = dataclasses.replace(options, max_output_tokens=agent.max_tokens_override)
        return self._adapter, options


def _is_stale(vault_root: Path, fm: dict, *, grace_days: int) -> bool:
    source_path = fm.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        return False
    root = vault_root.resolve()
    abs_path = (vault_root / source_path).resolve()
    # Bound to vault root — a `source_path` like `../../etc/passwd` must not
    # have its existence probed outside the vault. Treat escapes as stale so
    # the operator notices and corrects the frontmatter.
    try:
        abs_path.relative_to(root)
    except ValueError:
        return True
    if abs_path.exists():
        return False
    added_date = _coerce_date(fm.get("date_added"))
    if added_date is None:
        return True  # no grace anchor — stale once source disappears
    return (date.today() - added_date).days >= grace_days


def _coerce_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _build_contradiction_pairs(
    slugs: list[str],
    concepts_by_slug: dict[str, set[str]],
    cap: int,
) -> list[tuple[str, str, set[str]]]:
    """Every Source pair sharing ≥1 concept, in input order, capped."""
    out: list[tuple[str, str, set[str]]] = []
    for slug_a, slug_b in combinations(slugs, 2):
        shared = concepts_by_slug.get(slug_a, set()) & concepts_by_slug.get(slug_b, set())
        if not shared:
            continue
        out.append((slug_a, slug_b, shared))
        if len(out) >= cap:
            break
    return out


def _parse_verdict(raw: str) -> tuple[str, str]:
    parsed = _parse_llm_json(raw)
    verdict = str(parsed.get("verdict", "")).lower().strip()
    reason = str(parsed.get("reason", "")).strip()
    return verdict, reason


def _page_summary(manager: VaultManager, slug: str) -> str:
    fm = manager.read_wiki_page_frontmatter(slug)
    title = fm.get("title") or slug
    content = manager.read_wiki_page(slug) or ""
    if content.startswith("---"):
        _, _, rest = content.partition("---\n")
        _, _, body = rest.partition("---\n")
    else:
        body = content
    # Skip leading heading-only paragraphs (`# Title`) but keep the first
    # real paragraph. Earlier code blindly took paragraphs[1], which dropped
    # the summary when a page had no leading H1.
    paragraphs = [p.strip() for p in body.strip().split("\n\n") if p.strip()]
    summary = next((p for p in paragraphs if not p.startswith("#")), "")
    return f"Title: {title}\n{summary[:400]}"


@contextmanager
def _exclusive_lock(target: Path, *, timeout_s: float = 5.0, stale_after_s: float = 60.0):
    """Cross-process exclusive lock against `<target>.lock` via `O_EXCL`.

    Manual `vault_lint` invocations can overlap a scheduled run; without
    serialisation, the LINT-REPORT.md read-modify-write loses one writer's
    entries. Stdlib-only (no `fcntl` — Tesseract runs on Windows). A stale
    lock older than `stale_after_s` is reclaimed so a crashed process can't
    block all future runs forever.
    """
    lock_path = target.with_name(target.name + ".lock")
    deadline = _time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = _time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_after_s:
                try:
                    lock_path.unlink()
                    continue
                except OSError as exc:
                    logger.warning(
                        "vault_lint: could not reclaim stale lock %s: %s", lock_path, exc
                    )
            if _time.monotonic() > deadline:
                raise TimeoutError(f"vault_lint: contention on {lock_path}")
            _time.sleep(0.05)
    try:
        os.close(fd)
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _slugify_term(term: str) -> str:
    # Delegate to the canonical vault slugifier so lookup-side slugs match
    # the compile-time slugs emitted by `VaultLibrarian.compile_source`.
    # Without this, accented entities (e.g. "résumé") produced "r-sum-" on
    # the lookup side and "resume" on the compile side, causing lint to
    # fabricate missing-hub findings — audit-1 (2026-04-24) M7.
    from tesseract.memory.vault_manager import slugify
    return slugify(term)
