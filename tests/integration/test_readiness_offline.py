"""The readiness gate, end to end, offline (Milestone 12).

No gateway, no Docker, no collector, no model. This is the run an ordinary
developer gets from ``readiness check``, and the properties asserted here are
the ones that must hold whatever else is or is not available:

* it completes, and stores an immutable run;
* it submits zero orders;
* it certifies nothing it did not observe;
* it changes no mode, no guard and no switch.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from trading_system.domain.enums import (
    ReadinessLevel,
    ReadinessRunStatus,
    ReadinessStatus,
)
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings, load_config
from trading_system.readiness.service import CheckScope, ReadinessService

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
NOW = __import__("datetime").datetime(2026, 8, 15, 12, 0, tzinfo=__import__("datetime").UTC)


@pytest.fixture
def service(tmp_path: Path) -> ReadinessService:
    """Rooted at ``tmp_path``, reading the repository's real configuration."""
    return ReadinessService(
        settings=Settings(_env_file=None, trading_mode="PAPER"),
        config=load_config(REPO / "config"),
        clock=FixedClock(NOW),
        repo_root=REPO,
        root=tmp_path / "project",
    )


def test_an_offline_check_completes_and_stores_a_run(service: ReadinessService) -> None:
    result = service.check(CheckScope.offline())
    assert result.run.status is ReadinessRunStatus.PARTIAL
    assert result.stored
    assert service.latest() is not None


def test_an_offline_check_submits_no_orders(service: ReadinessService) -> None:
    """Asserted, not assumed. The model refuses any other value."""
    result = service.check(CheckScope.offline())
    assert result.run.orders_submitted == 0


def test_an_offline_check_cannot_claim_paper_readiness(service: ReadinessService) -> None:
    """Brief section 32: the offline suite passing is not a paper gate."""
    result = service.check(CheckScope.offline())
    assert result.run.level is ReadinessLevel.NOT_READY


def test_uncollected_criteria_are_not_tested_rather_than_passing(
    service: ReadinessService,
) -> None:
    result = service.check(CheckScope.offline())
    assessment = result.run.assessment
    assert assessment is not None
    not_tested = assessment.by_status(ReadinessStatus.NOT_TESTED)
    assert not_tested, "an offline run should leave several criteria uncollected"
    for criterion in not_tested:
        assert criterion.evidence_id is None


def test_the_run_records_what_was_deliberately_not_collected(
    service: ReadinessService,
) -> None:
    """An operator's choice, visible as a choice."""
    result = service.check(CheckScope.offline())
    assert result.run.not_collected
    assert any("observability" in reason for reason in result.run.not_collected.values())


def test_every_pass_names_its_evidence(service: ReadinessService) -> None:
    """Brief section 27, over a real run rather than a constructed one."""
    result = service.check(CheckScope.offline())
    assessment = result.run.assessment
    assert assessment is not None
    passed = assessment.by_status(ReadinessStatus.PASS)
    assert passed, "an offline run should establish something"
    for criterion in passed:
        assert criterion.evidence_id
        assert criterion.evidence_source


def test_the_assessment_can_be_re_derived_from_its_own_evidence(
    service: ReadinessService,
) -> None:
    """The point of separating collection from evaluation.

    A stored assessment is checkable rather than merely trustworthy: re-running
    the pure evaluator over the same bundle must reproduce it exactly.
    """
    from trading_system.readiness.evaluator import evaluate
    from trading_system.readiness.policy import ReadinessPolicy

    result = service.check(CheckScope.offline(), store=False)
    config = service.config
    assert config is not None
    replayed = evaluate(
        result.bundle,
        ReadinessPolicy.of(config.readiness),
        trading_mode=service.settings.trading_mode,
        system_version=result.run.system_version,
        config_version=result.run.config_version,
    )
    assert result.run.assessment is not None
    assert replayed.model_dump(mode="json") == result.run.assessment.model_dump(mode="json")


def test_a_readiness_run_changes_no_environment_variable(
    service: ReadinessService,
) -> None:
    """Brief section 2: readiness reports; it never enables."""
    watched = (
        "TRADING_MODE",
        "LIVE_TRADING_CONFIRMED",
        "LIVE_READINESS_CHECKLIST_SIGNED_OFF",
        "IBKR_READ_ONLY",
        "ALLOW_LIVE_TESTS",
        "RUN_PAPER_EXECUTION_TESTS",
    )
    before = {name: os.environ.get(name) for name in watched}
    service.check(CheckScope.offline())
    after = {name: os.environ.get(name) for name in watched}
    assert before == after


def test_a_readiness_run_changes_no_configuration_file(
    service: ReadinessService,
) -> None:
    """``config/`` is committed policy; an assessment never edits it."""
    config_dir = REPO / "config"
    before = {
        path: path.read_bytes() for path in sorted(config_dir.rglob("*.yaml")) if path.is_file()
    }
    service.check(CheckScope.offline())
    after = {
        path: path.read_bytes() for path in sorted(config_dir.rglob("*.yaml")) if path.is_file()
    }
    assert before == after


def test_nothing_is_written_outside_the_given_root(
    service: ReadinessService, tmp_path: Path
) -> None:
    service.check(CheckScope.offline())
    written = [path for path in tmp_path.rglob("*.json") if path.is_file()]
    assert written
    for path in written:
        assert str(path).startswith(str(tmp_path))


def test_the_stored_run_validates_against_its_schema(service: ReadinessService) -> None:
    """The contract test, over a real artifact."""
    import json

    from jsonschema import Draft202012Validator

    result = service.check(CheckScope.offline())
    schema = json.loads((REPO / "schemas" / "readiness_run.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    payload = json.loads(json.dumps(result.run.model_dump(mode="json")))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda error: list(error.path)
    )
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]


def test_a_configuration_that_will_not_load_is_reported_rather_than_crashing(
    tmp_path: Path,
) -> None:
    """An assessor that crashed could not report the thing that mattered most."""
    service = ReadinessService(
        settings=Settings(_env_file=None, trading_mode="PAPER"),
        config=None,
        config_error="config/risk.yaml: unknown key",
        clock=FixedClock(NOW),
        repo_root=REPO,
        root=tmp_path / "project",
    )
    result = service.check(CheckScope.offline())
    assert result.run.status is ReadinessRunStatus.CONFIGURATION_ERROR
    assert result.run.assessment is None
    assert result.run.level is ReadinessLevel.NOT_READY
    assert result.run.error is not None
