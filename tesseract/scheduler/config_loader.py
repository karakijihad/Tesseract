from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tesseract.lib.yaml_io import round_trip_yaml


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries: int
    backoff_seconds: int


class JobConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    cadence: str
    handler: str
    enabled: bool
    on_failure: Literal["log", "alert", "disable"]
    retry_policy: RetryPolicy
    config: dict[str, Any] = Field(default_factory=dict)
    # Optional override of the cognitive role this job's LLM call should
    # route through. Only meaningful for handlers that set
    # `uses_llm = True`; ignored otherwise. When `None`, the handler's
    # `default_model_role` is used. Validated against `roles.yaml::roles.*`
    # at engine boot — unknown role names raise loudly there, not here, so
    # an out-of-band yaml edit gets a clear error instead of a silent
    # fall-through. Persists via `persist_job_update` round-trip.
    model_role: str | None = None


class CatchupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Max catch-up jobs running concurrently at boot. A restart after a
    # missed window can queue 30+ jobs; firing them all at once saturates
    # the shared free-tier providers (2026-07-13: NIM 429 cascade).
    concurrency: int = Field(ge=1)


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catchup: CatchupPolicy
    jobs: list[JobConfig]


def load_schedule_config(config_dir: Path) -> ScheduleConfig:
    """Load and validate `<config_dir>/schedule.yaml`.

    Raises FileNotFoundError on missing file; pydantic.ValidationError on schema mismatch.
    No defaults — every required key must be present in the YAML.
    """
    path = config_dir / "schedule.yaml"
    if not path.exists():
        raise FileNotFoundError(f"schedule.yaml not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ScheduleConfig.model_validate(raw)


def persist_job_update(
    config_dir: Path,
    job_name: str,
    updates: dict[str, Any],
) -> None:
    """Round-trip `schedule.yaml` with `updates` applied to the named job.

    Preserves operator comments and key order by reading + writing via
    `tesseract/lib/yaml_io.round_trip_yaml`. Raises KeyError if `job_name`
    is not in the file; FileNotFoundError if the yaml is missing.
    """
    path = config_dir / "schedule.yaml"
    if not path.exists():
        raise FileNotFoundError(f"schedule.yaml not found at {path}")

    def _apply(doc: Any) -> None:
        for job in (doc.get("jobs") or []):
            if job.get("name") == job_name:
                for key, value in updates.items():
                    job[key] = value
                return
        raise KeyError(job_name)

    round_trip_yaml(path, _apply)


def persist_job_add(config_dir: Path, job_cfg: JobConfig) -> None:
    """Phase 18 Task B — append a new job to `schedule.yaml`.

    Idempotent on name collision (raises ValueError). Comments + ordering
    of existing jobs preserved via `round_trip_yaml`. The new entry
    serializes via `model_dump()` and lands at the end of the `jobs` list.
    """
    path = config_dir / "schedule.yaml"
    if not path.exists():
        raise FileNotFoundError(f"schedule.yaml not found at {path}")

    def _apply(doc: Any) -> None:
        jobs = doc.get("jobs")
        if jobs is None:
            doc["jobs"] = []
            jobs = doc["jobs"]
        for existing in jobs:
            if existing.get("name") == job_cfg.name:
                raise ValueError(f"job {job_cfg.name!r} already exists in schedule.yaml")
        jobs.append(job_cfg.model_dump())

    round_trip_yaml(path, _apply)


def persist_job_remove(config_dir: Path, job_name: str) -> None:
    """Phase 18 Task B — remove a named job from `schedule.yaml`.

    Raises KeyError if the name is not present. Comments + ordering of
    surviving jobs preserved via `round_trip_yaml`.
    """
    path = config_dir / "schedule.yaml"
    if not path.exists():
        raise FileNotFoundError(f"schedule.yaml not found at {path}")

    def _apply(doc: Any) -> None:
        jobs = doc.get("jobs") or []
        for idx, existing in enumerate(jobs):
            if existing.get("name") == job_name:
                del jobs[idx]
                return
        raise KeyError(job_name)

    round_trip_yaml(path, _apply)
