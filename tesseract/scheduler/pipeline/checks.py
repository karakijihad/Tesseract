"""The two boot checks that are about config rather than the stage graph.

Both describe a defect this repo shipped and nobody noticed, because in each
case the missing half is silent by construction:

- an enabled agenda mapper whose source nothing publishes never fires, and a
  source that never fires looks exactly like a quiet one;
- a daily cap read for enforcement against a field nothing writes reads zero
  forever, so the ceiling in the config was never a ceiling.

These report rather than raise. Three producers are missing on this tree right
now (AR-7 decides whether each is wired or deleted), and a backend that refuses
to boot until then would be a worse answer than one that says so every time.
The graph checks in `graph.py` DO raise: those cover a declaration this code
owns, where a partial run has no safe meaning.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Which `daily_caps` key is populated by which writer. A cap with no entry
# here, or one whose writer no longer resolves, is a ceiling that cannot be
# reached — the state `daily_caps.tokens` and `.seconds` were in until AR-1.
# The `schedule.yaml` rows that run a pipeline row. If the declaration does
# not hold, these are the jobs to disable — and only these: every other row on
# the machine is unaffected by a bad edge in the pipeline graph.
PIPELINE_ROW_JOBS: tuple[str, ...] = ("capture", "consolidate")

CAP_WRITERS: dict[str, str] = {
    "tokens": "tesseract.orchestrator.autonomy.spend_ledger:record_spend",
    "seconds": "tesseract.orchestrator.autonomy.spend_ledger:record_spend",
    "usd": "tesseract.brain.cost.ledger:CostLedger",
}


@dataclass(frozen=True)
class CheckFinding:
    check: str
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.subject}: {self.detail}"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def _enabled_job_names(schedule_yaml: Path) -> set[str]:
    """What is armed, by name — rows AND the stages of an enabled row.

    A job that moves into the anchor is still scheduled; it is a stage now.
    Reading only row names would report `provider_probe` as having no producer
    the moment it stopped being a row, which is the opposite of what happened
    to it — and the producer check would then be measuring the file's shape
    rather than whether the work runs.

    A stage is armed unless its own config block says `enabled: false`, which
    is the same switch the row honours.
    """
    from tesseract.scheduler.pipeline.registry import row as pipeline_row
    from tesseract.scheduler.pipeline import stages  # noqa: F401 — registers

    raw = _load_yaml(schedule_yaml)
    names: set[str] = set()
    for job in raw.get("jobs") or []:
        if not isinstance(job, dict) or not job.get("enabled"):
            continue
        name = str(job.get("name"))
        names.add(name)
        declared = pipeline_row(name)
        if declared is None:
            continue
        config = job.get("config") if isinstance(job.get("config"), dict) else {}
        for stage in declared.stages:
            block = config.get(stage.name)
            if isinstance(block, dict) and block.get("enabled") is False:
                continue
            names.add(stage.name)
    return names


def check_mapper_producers(
    *, mappers_yaml: Path, schedule_yaml: Path
) -> list[CheckFinding]:
    """Boot check 2 — every enabled agenda mapper has an enabled producer.

    The producer table is imported here rather than at module load on purpose.
    A long-running backend that hot-reloads `schedule.yaml` re-imports the row
    handlers against ALREADY-CACHED modules, so a package-level import of
    something added to `mappers` in the same change fails — and takes the
    handler import down with it, leaving the row disabled and the operator
    reading "not yet available" about code that is right there on disk.
    Observed exactly that on 2026-08-15. A check may fail to run; it may not
    stop the work it checks.
    """
    from tesseract.orchestrator.autonomy.mappers import SOURCE_PRODUCERS

    mappers = _load_yaml(mappers_yaml).get("mappers") or {}
    jobs = _enabled_job_names(schedule_yaml)
    findings: list[CheckFinding] = []
    for key, entry in mappers.items():
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        source = str(entry.get("source") or key)
        producers = next(
            (v for k, v in SOURCE_PRODUCERS.items() if k.value == source), None
        )
        if producers is None:
            findings.append(
                CheckFinding(
                    "mapper_producer",
                    source,
                    "mapper is enabled but the source is not in SOURCE_PRODUCERS — "
                    "declare what publishes it in orchestrator/autonomy/mappers/__init__.py",
                )
            )
            continue
        if not producers:
            findings.append(
                CheckFinding(
                    "mapper_producer",
                    source,
                    "mapper is enabled and nothing publishes this source",
                )
            )
            continue
        live = [p for p in producers if not p.startswith("job:")]
        scheduled = [p.removeprefix("job:") for p in producers if p.startswith("job:")]
        if live:
            continue
        if not any(name in jobs for name in scheduled):
            findings.append(
                CheckFinding(
                    "mapper_producer",
                    source,
                    "mapper is enabled but none of its producers "
                    f"({', '.join(sorted(scheduled))}) is an enabled job in schedule.yaml",
                )
            )
    return findings


def _resolves(target: str) -> bool:
    module_path, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        return False
    return hasattr(module, attr)


def check_budget_writers(*, agenda_yaml: Path) -> list[CheckFinding]:
    """Boot check 3 — every enforced budget cap has a writer that populates it."""
    caps = _load_yaml(agenda_yaml).get("daily_caps") or {}
    findings: list[CheckFinding] = []
    for key, value in caps.items():
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        writer = CAP_WRITERS.get(str(key))
        if writer is None:
            findings.append(
                CheckFinding(
                    "budget_writer",
                    f"daily_caps.{key}",
                    f"enforced at {value} but nothing is declared to populate it",
                )
            )
        elif not _resolves(writer):
            findings.append(
                CheckFinding(
                    "budget_writer",
                    f"daily_caps.{key}",
                    f"its declared writer {writer} no longer resolves",
                )
            )
    return findings


def check_declared_rows() -> list[CheckFinding]:
    """The graph checks, where a failure disables the pipeline and nothing else.

    `validate_rows` raises on a cycle or a read no row produces. Called at
    import of the stages package, that exception escaped the engine's
    constructor and left the machine with no scheduler at all. Here it is a
    finding: the rows are refused, every other scheduled job still runs.
    """
    from tesseract.scheduler.pipeline.graph import PipelineDeclarationError
    from tesseract.scheduler.pipeline.registry import rows, validate_rows

    try:
        import tesseract.scheduler.pipeline.stages  # noqa: F401 — registers the rows
        validate_rows()
    except PipelineDeclarationError as exc:
        return [CheckFinding("row_declaration", "pipeline", str(exc))]
    except Exception as exc:  # an import error in a stage module reads the same way
        return [CheckFinding("row_declaration", "pipeline", f"rows unavailable: {exc!r}")]
    if not rows():
        return [CheckFinding("row_declaration", "pipeline", "no rows are registered")]
    return []


def check_stage_model_roles(*, roles_yaml: Path, schedule_yaml: Path) -> list[CheckFinding]:
    """Every per-stage `model_role` names a role that exists.

    The engine validates a ROW's `model_role` at boot and raises on a name
    `roles.yaml` does not define. Collapsing eighteen rows moved four of those
    overrides into per-stage config blocks, which that validator never sees —
    and the failure is silent rather than loud: `build_chain_for_role` returns
    an EMPTY chain for an unknown role, the wrapped job treats that as "skip
    this run", and the stage reports `skipped_no_work`, the least alarming
    outcome there is. A typo would read as a quiet night indefinitely.
    """
    from tesseract.scheduler.pipeline.registry import row as get_row

    roles = (_load_yaml(roles_yaml).get("roles") or {}) if roles_yaml.exists() else {}
    findings: list[CheckFinding] = []
    for job in _load_yaml(schedule_yaml).get("jobs") or []:
        if not isinstance(job, dict):
            continue
        declared = get_row(str(job.get("name")))
        stage_names = {s.name for s in declared.stages} if declared else None
        for stage_name, block in (job.get("config") or {}).items():
            if not isinstance(block, dict):
                continue
            # A config block keyed by a stage that does not exist is settings
            # nothing reads — a rename or a typo, and it looks configured. Only
            # checked for rows that ARE pipeline rows; every other job's config
            # is its own private shape.
            if stage_names is not None and stage_name not in stage_names:
                findings.append(
                    CheckFinding(
                        "stage_config_key",
                        f"{job.get('name')}.{stage_name}",
                        "config block names no stage in this row — its settings, "
                        f"including any model_role, reach nothing "
                        f"(stages: {', '.join(sorted(stage_names))})",
                    )
                )
                continue
            role = block.get("model_role")
            if role and role not in roles:
                findings.append(
                    CheckFinding(
                        "stage_model_role",
                        f"{job.get('name')}.{stage_name}",
                        f"model_role {role!r} is not defined in roles.yaml — the stage "
                        "would run with no adapter and report a quiet night",
                    )
                )
    return findings


def run_config_checks(config_dir: Path) -> list[CheckFinding]:
    """Every check, in one call, for boot and for CI."""
    return [
        *check_declared_rows(),
        *check_mapper_producers(
            mappers_yaml=config_dir / "agenda-mappers.yaml",
            schedule_yaml=config_dir / "schedule.yaml",
        ),
        *check_budget_writers(agenda_yaml=config_dir / "agenda.yaml"),
        *check_stage_model_roles(
            roles_yaml=config_dir / "roles.yaml",
            schedule_yaml=config_dir / "schedule.yaml",
        ),
    ]


def log_config_checks(config_dir: Path) -> list[CheckFinding]:
    """Run both checks and say what they found, once, at boot."""
    findings = run_config_checks(config_dir)
    for finding in findings:
        log.error("pipeline boot check: %s", finding)
    return findings


__all__ = [
    "CAP_WRITERS",
    "PIPELINE_ROW_JOBS",
    "CheckFinding",
    "check_budget_writers",
    "check_declared_rows",
    "check_mapper_producers",
    "check_stage_model_roles",
    "log_config_checks",
    "run_config_checks",
]
