"""AU-3 S2 — WorkerRetryPolicy decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.retry import (
    DEFAULT_RETRY,
    WorkerRetryPolicy,
    WorkerRetryRule,
)


def _build(policy_dict: dict) -> WorkerRetryPolicy:
    return WorkerRetryPolicy.from_mission_lanes_block(policy_dict)


def test_default_rule_no_retries(isolated_home: Path) -> None:
    policy = _build({"claude_cli": 1})  # int shape — no retry
    rule = policy.rule_for(WorkerKind.CLAUDE_CLI)
    assert rule == DEFAULT_RETRY
    decision = policy.decide(kind=WorkerKind.CLAUDE_CLI, error_class="transient_network", retry_count=0)
    assert not decision.retry
    assert decision.reason == "max_retries_reached"


def test_give_up_short_circuits(isolated_home: Path) -> None:
    policy = _build(
        {
            "claude_cli": {
                "max_concurrent": 1,
                "retry": {
                    "max_retries": 5,
                    "retry_on_classes": ["transient_network", "permission_denied"],
                    "give_up_on_classes": ["permission_denied"],
                },
            }
        }
    )
    decision = policy.decide(
        kind=WorkerKind.CLAUDE_CLI,
        error_class="permission_denied",
        retry_count=0,
    )
    assert not decision.retry
    assert decision.reason == "give_up:permission_denied"


def test_retry_on_eligible_class(isolated_home: Path) -> None:
    policy = _build(
        {
            "claude_cli": {
                "max_concurrent": 1,
                "retry": {
                    "max_retries": 2,
                    "backoff_seconds": 30,
                    "retry_on_classes": ["transient_network"],
                    "give_up_on_classes": [],
                },
            }
        }
    )
    decision = policy.decide(
        kind=WorkerKind.CLAUDE_CLI,
        error_class="transient_network",
        retry_count=0,
    )
    assert decision.retry
    assert decision.backoff_seconds == 30.0
    assert decision.reason == "retry:transient_network"


def test_retry_count_caps(isolated_home: Path) -> None:
    policy = _build(
        {
            "claude_cli": {
                "max_concurrent": 1,
                "retry": {
                    "max_retries": 2,
                    "retry_on_classes": ["transient_network"],
                },
            }
        }
    )
    decision = policy.decide(
        kind=WorkerKind.CLAUDE_CLI,
        error_class="transient_network",
        retry_count=2,
    )
    assert not decision.retry
    assert decision.reason == "max_retries_reached"


def test_unconfigured_class_does_not_retry(isolated_home: Path) -> None:
    policy = _build(
        {
            "claude_cli": {
                "max_concurrent": 1,
                "retry": {
                    "max_retries": 5,
                    "retry_on_classes": ["transient_network"],
                },
            }
        }
    )
    decision = policy.decide(
        kind=WorkerKind.CLAUDE_CLI,
        error_class="some_other_class",
        retry_count=0,
    )
    assert not decision.retry
    assert "class_not_eligible" in decision.reason


def test_unknown_kind_falls_to_default(isolated_home: Path) -> None:
    policy = _build({"claude_cli": {"max_concurrent": 1, "retry": {"max_retries": 5}}})
    decision = policy.decide(
        kind=WorkerKind.TARS_SELF,
        error_class="transient_network",
        retry_count=0,
    )
    assert not decision.retry
    assert decision.reason == "max_retries_reached"


def test_malformed_block_falls_to_default(isolated_home: Path) -> None:
    """A YAML typo (retry as a string) must not deadlock the lane."""
    policy = _build(
        {
            "claude_cli": {
                "max_concurrent": 1,
                "retry": "this should be a dict",
            }
        }
    )
    assert policy.rule_for(WorkerKind.CLAUDE_CLI) == DEFAULT_RETRY
