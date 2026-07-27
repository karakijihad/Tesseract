"""Janitor result shapes."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    """One thing the janitor saw. `action` is what it did about it:
    "killed" / "removed" / "closed" / "pruned" on success, "would-<verb>"
    under dry-run, "failed" when the attempt errored (detail says why)."""

    sweep: str  # "processes" | "scratch" | "sessions" | "archives"
    target: str
    action: str
    detail: str = ""


@dataclass(frozen=True)
class SweepReport:
    started_at_utc: str
    finished_at_utc: str
    dry_run: bool
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        by_sweep: dict[str, int] = {}
        for f in self.findings:
            by_sweep[f.sweep] = by_sweep.get(f.sweep, 0) + 1
        parts = [f"{k}={v}" for k, v in sorted(by_sweep.items())]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        mode = "dry-run" if self.dry_run else "applied"
        return f"{mode}: " + (" ".join(parts) if parts else "nothing to clean")
