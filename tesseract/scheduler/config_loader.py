"""Loading and persisting the scheduler's job registry.

**Two files, and the order between them is the ownership rule (AR-6).** The
shipped rows live in the sealed app tree (`paths.system_config_dir()`) and are
read, never copied; the operator's live in `home/config/schedule.yaml`. A user
row whose `name` matches a shipped one OVERRIDES it field by field; a user row
with a new name is a job of their own.

That split is what makes a new shipped job reach an existing install at all.
`config_seed.migrate_config_keys` merges new KEYS into files the operator
already has, but `jobs` is a list and a list is copied whole or not at all —
the key exists on every install, so nothing inside it was ever merged. Three
rows added by earlier phases would have landed on no existing machine.

`handler`, `when` and `summary` are the fields an override may not set. They are
what the job IS: letting a data file in the operator's tree re-point a shipped
job's import path is a code-execution redirect wearing a config edit's clothes,
re-pointing which event fires it is the same move one level out, and what a
shipped row is FOR is the app's own claim, kept in the run manifest. The
threshold that event is judged against (`when_config`) is theirs, as `cadence`
is.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tesseract import paths
from tesseract.lib.yaml_io import round_trip_yaml

log = logging.getLogger(__name__)


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries: int
    backoff_seconds: int


class JobConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    # A row fires on a clock OR on an event, and declares exactly one of the
    # two. `cadence` is a cron expression or interval shorthand; `when` names a
    # condition in `scheduler/triggers/conditions.py`, whose thresholds are
    # `when_config`. Kept as two fields rather than one string with a sigil
    # because everything that reads a cadence — croniter, the catch-up
    # computation, the Schedule tab — has to know which it is looking at, and a
    # field that is sometimes cron and sometimes not is how that check gets
    # skipped.
    cadence: str = ""
    when: str = ""
    when_config: dict[str, Any] = Field(default_factory=dict)
    handler: str
    enabled: bool
    # One line saying what this row is for, in the operator's words. Shipped
    # rows carry theirs in the run manifest — what the app's own work is for is
    # not a data file's to redefine — so this is the field an OPERATOR row
    # fills, and the tracker renders a row that has none as a gap rather than
    # refusing to boot over it. The two doors we own require it; a hand-edited
    # file is reported, never refused.
    summary: str = ""
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

    @model_validator(mode="after")
    def _one_firing_rule(self) -> "JobConfig":
        if bool(self.cadence.strip()) == bool(self.when.strip()):
            stated = "both a cadence and a when" if self.cadence.strip() else "neither"
            raise ValueError(
                f"job {self.name!r} declares {stated}. A row fires on a clock or on "
                "an event: give it `cadence:` or `when:`, exactly one"
            )
        if self.when_config and not self.when.strip():
            raise ValueError(
                f"job {self.name!r} has a `when_config` and no `when` — thresholds "
                "for a firing rule it does not have"
            )
        return self


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


class JobOverride(BaseModel):
    """What an operator row may say about a job the app ships.

    Every field of `JobConfig` except `handler`, and all of them optional: an
    absent field means "keep following the shipped value", which is the whole
    point — a cadence we correct in a release reaches an operator who only
    ever toggled `enabled`.

    `extra="forbid"` makes `handler` a loud error rather than a silent
    ignore. A quietly-dropped handler override would leave the operator
    believing they had re-pointed a job.

    `when` is the second field an override may not set, for the same reason as
    `handler`: which event fires a job is what the job IS, not how often the
    operator wants it. What they may tune is `when_config` — the threshold —
    which is the same kind of preference `cadence` is.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    cadence: str | None = None
    when_config: dict[str, Any] | None = None
    enabled: bool | None = None
    on_failure: Literal["log", "alert", "disable"] | None = None
    retry_policy: RetryPolicy | None = None
    config: dict[str, Any] | None = None
    model_role: str | None = None

    def applied_to(self, base: JobConfig) -> JobConfig:
        stated = self.model_dump(exclude_none=True, exclude={"name"})
        # Re-validated rather than copied, so an override that gives a
        # trigger row a cadence is refused here instead of producing a row
        # with two firing rules and no rule about which one wins.
        return JobConfig.model_validate({**base.model_dump(), **stated})


# Set once here rather than derived from `JobOverride`'s fields at each call
# site, so "what an override may not touch" is one readable line. `summary` is
# the third for the same reason as the first two: what a shipped row is FOR is
# the app's claim about its own work, and it lives in the run manifest.
OVERRIDE_FORBIDDEN_FIELDS = frozenset({"handler", "when", "summary"})


def system_schedule_path() -> Path:
    """The shipped `schedule.yaml`, in the app tree."""
    return paths.system_config_dir() / "schedule.yaml"


def _config_roots(config_dir: Path) -> tuple[Path | None, Path]:
    """`(user_dir_or_None, system_dir)` for a caller's `config_dir`.

    Naming the LIVE config dir means the running install, so it merges with
    the app tree; any other directory is read alone.

    Unlike `agents/loader._roots` — which takes `None` for "the live pair" —
    this cannot, and the difference is not stylistic. `SchedulerEngine` holds
    one `config_dir` used for BOTH loading and persisting, and its tests
    construct engines over a tmp tree. If an explicit directory always merged,
    a test's four-line yaml would inherit sixteen shipped jobs; if it never
    did, production would lose them. The live-dir test is what separates the
    two without giving the engine a second path to carry.

    A dev checkout has one config tree, so `user_dir` is None there and the
    single file is read as shipped — which is also why `system_job_names()`
    returns nothing in dev: those rows are the operator's own source, and
    sealing them would refuse to remove a job in the repo being developed.
    """
    system = paths.system_config_dir()
    try:
        is_live = config_dir.resolve() == paths.config_dir().resolve()
    except OSError:
        is_live = config_dir == paths.config_dir()
    if not is_live or config_dir.resolve() == system.resolve():
        return None, config_dir
    return config_dir, system


def _read_yaml(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping, got {type(raw).__name__}")
    return raw


def load_schedule_config(config_dir: Path) -> ScheduleConfig:
    """Load and validate the schedule, merging the operator's rows over the
    shipped ones.

    Raises FileNotFoundError when the file a root must have is missing;
    pydantic.ValidationError on schema mismatch. No defaults — every required
    key must be present in the shipped YAML.

    The user file is optional. A fresh install has none, and the whole
    registry resolves out of the app tree.
    """
    user_dir, system_dir = _config_roots(config_dir)
    system_path = system_dir / "schedule.yaml"
    if not system_path.exists():
        raise FileNotFoundError(f"schedule.yaml not found at {system_path}")

    if user_dir is None:
        return ScheduleConfig.model_validate(_read_yaml(system_path))

    system_raw = _read_yaml(system_path)
    shipped = ScheduleConfig.model_validate(system_raw)
    by_name = {job.name: job for job in shipped.jobs}

    user_path = user_dir / "schedule.yaml"
    if not user_path.exists():
        return shipped

    user_raw = _read_yaml(user_path)
    catchup = (
        CatchupPolicy.model_validate(user_raw["catchup"])
        if isinstance(user_raw.get("catchup"), dict)
        else shipped.catchup
    )

    merged: list[JobConfig] = list(shipped.jobs)
    positions = {job.name: index for index, job in enumerate(merged)}
    for row in user_raw.get("jobs") or []:
        if not isinstance(row, dict):
            raise ValueError(f"{user_path}: every entry under `jobs` must be a mapping")
        name = row.get("name")
        base = by_name.get(name)
        if base is None:
            if "handler" not in row:
                # An ORPHANED OVERRIDE. `persist_job_update` writes the name
                # plus the fields that changed, so a row the operator disabled
                # or re-tuned is two keys — and once a release stops shipping
                # the row it names, there is nothing left to apply it to.
                # Validating it as a job of their own asks for `handler`,
                # `on_failure` and `retry_policy`, which an override never
                # carries, and that raise took down the WHOLE schedule rather
                # than the row that went away. Named and skipped instead: the
                # app removing its own row is not the operator's error, and no
                # other row should stop firing over it.
                log.warning(
                    "scheduler: %s carries an override for %r, which this "
                    "version no longer ships — ignoring it. Delete the row to "
                    "silence this.",
                    user_path, name,
                )
                continue
            merged.append(JobConfig.model_validate(row))
            continue
        # A forbidden key that merely RESTATES the shipped value is the
        # residue of the old whole-file copy — every install has it, and
        # refusing it would refuse to boot. What must raise is a key that
        # actually changes the job.
        stated = {
            key: value for key, value in row.items()
            if key not in OVERRIDE_FORBIDDEN_FIELDS
            or value != getattr(base, key, None)
        }
        forbidden = OVERRIDE_FORBIDDEN_FIELDS & set(stated)
        if forbidden:
            raise ValueError(
                f"{user_path}: job {name!r} is set by the app, so it cannot "
                f"override {sorted(forbidden)}. Remove the key; to stop it "
                "firing, set `enabled: false`."
            )
        merged[positions[name]] = JobOverride.model_validate(stated).applied_to(base)

    return ScheduleConfig(catchup=catchup, jobs=merged)


def system_job_names(config_dir: Path | None = None) -> set[str]:
    """Names the app ships. Empty when the caller named a sandbox, since
    nothing there is the app's."""
    user_dir, system_dir = _config_roots(config_dir or paths.config_dir())
    if user_dir is None:
        return set()
    path = system_dir / "schedule.yaml"
    if not path.exists():
        return set()
    return {
        str(row.get("name")) for row in (_read_yaml(path).get("jobs") or [])
        if isinstance(row, dict) and row.get("name")
    }


def _ensure_user_schedule(path: Path) -> None:
    """Create the operator's `schedule.yaml` if they have none.

    A fresh install has no file here at all — the whole registry resolves out
    of the app tree — so the first toggle is what brings one into existence.
    It carries `jobs` only: `catchup` stays the app's until the operator has
    a reason of their own to state one.
    """
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Your scheduler rows.\n"
        "#\n"
        "# A row whose `name` matches one the app ships OVERRIDES it, field by\n"
        "# field — anything you leave out keeps following the app, so a cadence\n"
        "# corrected in an update still reaches you. A row with a new name is a\n"
        "# job of your own. `handler` cannot be overridden.\n"
        "jobs: []\n",
        encoding="utf-8",
    )


def persist_job_update(
    config_dir: Path,
    job_name: str,
    updates: dict[str, Any],
) -> None:
    """Round-trip the operator's `schedule.yaml` with `updates` applied.

    Preserves operator comments and key order by reading + writing via
    `tesseract/lib/yaml_io.round_trip_yaml`.

    On a job the app ships this writes an OVERRIDE row — the name plus the
    fields that changed — creating it if the operator has none yet. It never
    copies the shipped row, because a copy is what stops the next release's
    correction from arriving, and a toggle is not a request to be frozen.

    Raises KeyError if the name is neither an operator row nor a shipped one;
    ValueError on an attempt to override `handler`.
    """
    forbidden = OVERRIDE_FORBIDDEN_FIELDS & set(updates)
    system_names = system_job_names(config_dir)
    if forbidden and job_name in system_names:
        raise ValueError(
            f"job {job_name!r} is set by the app, so {sorted(forbidden)} cannot "
            "be changed. To stop it firing, disable it."
        )

    path = config_dir / "schedule.yaml"
    if job_name in system_names:
        _ensure_user_schedule(path)
    elif not path.exists():
        raise FileNotFoundError(f"schedule.yaml not found at {path}")

    def _apply(doc: Any) -> None:
        jobs = doc.get("jobs")
        if jobs is None:
            doc["jobs"] = []
            jobs = doc["jobs"]
        for job in jobs:
            if job.get("name") == job_name:
                for key, value in updates.items():
                    job[key] = value
                return
        if job_name not in system_names:
            raise KeyError(job_name)
        jobs.append({"name": job_name, **updates})

    round_trip_yaml(path, _apply)


def persist_job_add(config_dir: Path, job_cfg: JobConfig) -> None:
    """Phase 18 Task B — append a new job to the operator's `schedule.yaml`.

    Raises ValueError on a name collision, with a shipped job counting as a
    collision: a second row of that name would be read as an override, so
    "adding" it would silently reconfigure the app's job instead.
    """
    path = config_dir / "schedule.yaml"
    if job_cfg.name in system_job_names(config_dir):
        raise ValueError(
            f"job {job_cfg.name!r} is already a job the app ships. Pick another "
            "name, or change that one rather than adding a second."
        )
    _ensure_user_schedule(path)

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
    """Phase 18 Task B — remove a named job from the operator's `schedule.yaml`.

    A job the app ships cannot be removed, and the error says what to do
    instead. Deleting the operator's row would only drop their overrides: the
    row itself lives in the app tree and the job would be back, on the shipped
    cadence, at the next boot. A removal that silently un-does itself is worse
    than a refusal.

    Raises KeyError if the name is not present. Comments + ordering of
    surviving jobs preserved via `round_trip_yaml`.
    """
    if job_name in system_job_names(config_dir):
        raise ValueError(
            f"job {job_name!r} is set by the app and cannot be removed — it "
            "would return on the next start. Disable it instead."
        )
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
