"""The readiness policy: which criteria block which level, and how it loads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trading_system.domain.enums import ReadinessCriterionId, ReadinessLevel
from trading_system.infrastructure.settings import (
    ReadinessConfig,
    ReadinessLevelConfig,
    ReadinessLevelsConfig,
    ReadinessPaperExecutionConfig,
    ReadinessSignoffConfig,
)
from trading_system.readiness.policy import ReadinessPolicy

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The live gate is never weaker than the paper gate
# ---------------------------------------------------------------------------
def test_live_review_requires_everything_paper_does(policy: ReadinessPolicy) -> None:
    """Brief section 30. A live gate that dropped a paper requirement would be
    the weaker gate wearing the stronger name."""
    assert policy.paper_blocking < policy.live_review_blocking


def test_the_shipped_policy_blocks_on_the_observability_stack(
    policy: ReadinessPolicy,
) -> None:
    """Brief section 31: M11's NOT_TESTED backends are a named M12 gate."""
    for criterion in (
        ReadinessCriterionId.OBSERVABILITY_STACK_RUNNING,
        ReadinessCriterionId.COLLECTOR_RECEIVES_TELEMETRY,
        ReadinessCriterionId.TEMPO_HAS_TRACE,
        ReadinessCriterionId.PROMETHEUS_HAS_METRIC,
        ReadinessCriterionId.LOKI_HAS_LOG,
        ReadinessCriterionId.GRAFANA_RUNNING,
        ReadinessCriterionId.GRAFANA_DATASOURCES_PROVISIONED,
        ReadinessCriterionId.GRAFANA_DASHBOARDS_PROVISIONED,
        ReadinessCriterionId.TRACE_LOG_CORRELATION,
    ):
        assert criterion in policy.paper_blocking, f"{criterion.value} does not block paper"


def test_the_shipped_policy_blocks_on_the_paper_gateway(policy: ReadinessPolicy) -> None:
    """Brief section 32: no reachable gateway, no READY_FOR_PAPER."""
    assert ReadinessCriterionId.PAPER_BROKER_REACHABLE in policy.paper_blocking
    assert ReadinessCriterionId.ACCOUNT_READABLE in policy.paper_blocking


def test_a_dirty_tree_blocks_only_live_review(policy: ReadinessPolicy) -> None:
    """Brief section 29: blocking for live review, reported for paper."""
    assert ReadinessCriterionId.WORKING_TREE_CLEAN not in policy.paper_blocking
    assert ReadinessCriterionId.WORKING_TREE_CLEAN in policy.live_review_blocking


def test_operational_history_blocks_only_live_review(policy: ReadinessPolicy) -> None:
    """Brief section 21: "it works" is enough for paper, not for a live review."""
    assert ReadinessCriterionId.OPERATIONAL_HISTORY_SUFFICIENT not in policy.paper_blocking
    assert ReadinessCriterionId.OPERATIONAL_HISTORY_SUFFICIENT in policy.live_review_blocking


def test_blocking_levels_are_reported_in_ascending_order(policy: ReadinessPolicy) -> None:
    levels = policy.blocking_levels(ReadinessCriterionId.TEST_SUITE_PASSES)
    assert levels == (ReadinessLevel.READY_FOR_PAPER, ReadinessLevel.READY_FOR_LIVE_REVIEW)


def test_a_live_only_criterion_reports_one_level(policy: ReadinessPolicy) -> None:
    levels = policy.blocking_levels(ReadinessCriterionId.WORKING_TREE_CLEAN)
    assert levels == (ReadinessLevel.READY_FOR_LIVE_REVIEW,)


# ---------------------------------------------------------------------------
# Freshness classification
# ---------------------------------------------------------------------------
def test_test_results_are_bound_to_a_revision(policy: ReadinessPolicy) -> None:
    assert policy.is_revision_bound(ReadinessCriterionId.TEST_SUITE_PASSES)


def test_a_broker_probe_is_bound_to_a_clock(policy: ReadinessPolicy) -> None:
    assert not policy.is_revision_bound(ReadinessCriterionId.PAPER_BROKER_REACHABLE)
    assert policy.window_seconds("broker_seconds") == 900.0


def test_an_unknown_window_name_resolves_to_nothing(policy: ReadinessPolicy) -> None:
    assert policy.window_seconds("not_a_window") is None
    assert policy.window_seconds(None) is None


# ---------------------------------------------------------------------------
# Configuration refusals
# ---------------------------------------------------------------------------
def test_a_live_gate_that_narrows_the_paper_gate_fails_to_load() -> None:
    """A criterion in the paper set and not the live one is refused outright."""
    with pytest.raises(ValidationError, match="does not require"):
        ReadinessConfig(
            levels=ReadinessLevelsConfig(
                paper=ReadinessLevelConfig(blocking=(ReadinessCriterionId.TEST_SUITE_PASSES,)),
                live_review=ReadinessLevelConfig(blocking=(), inherits_paper=False),
            )
        )


def test_an_explicit_superset_is_accepted_without_inheritance() -> None:
    config = ReadinessConfig(
        levels=ReadinessLevelsConfig(
            paper=ReadinessLevelConfig(blocking=(ReadinessCriterionId.TEST_SUITE_PASSES,)),
            live_review=ReadinessLevelConfig(
                blocking=(
                    ReadinessCriterionId.TEST_SUITE_PASSES,
                    ReadinessCriterionId.WORKING_TREE_CLEAN,
                ),
                inherits_paper=False,
            ),
        )
    )
    assert config.blocking_for_paper() < config.blocking_for_live_review()


def test_submitting_on_a_readiness_check_fails_to_load() -> None:
    """There is no configuration in which reporting places an order."""
    with pytest.raises(ValidationError, match="submit_on_check"):
        ReadinessPaperExecutionConfig(submit_on_check=True)


def test_a_live_paper_gate_fails_to_load() -> None:
    with pytest.raises(ValidationError, match="allow_live"):
        ReadinessPaperExecutionConfig(allow_live=True)


def test_more_than_one_order_fails_to_load() -> None:
    """Brief section 34: an uncontrolled sequence of orders is the failure mode."""
    with pytest.raises(ValidationError):
        ReadinessPaperExecutionConfig(max_orders=2)


def test_a_signoff_that_enables_trading_fails_to_load() -> None:
    with pytest.raises(ValidationError, match="enables_trading"):
        ReadinessSignoffConfig(enables_trading=True)


def test_an_unknown_configuration_key_is_refused() -> None:
    """``extra="forbid"``: a typo fails loudly rather than silently doing nothing."""
    with pytest.raises(ValidationError):
        ReadinessSignoffConfig(require_explicit_identiy=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The shipped configuration
# ---------------------------------------------------------------------------
def test_the_paper_execution_gate_ships_disabled(readiness_config: ReadinessConfig) -> None:
    """Submitting a real order is a deliberate act, never a shipped default."""
    assert readiness_config.paper_execution.enabled is False
    assert readiness_config.paper_execution.submit_on_check is False
    assert readiness_config.paper_execution.allow_live is False
    assert readiness_config.paper_execution.max_orders == 1


def test_the_shipped_signoff_policy_requires_an_explicit_identity(
    readiness_config: ReadinessConfig,
) -> None:
    assert readiness_config.signoff.require_explicit_identity is True
    assert readiness_config.signoff.require_live_review is True
    assert readiness_config.signoff.require_clean_working_tree is True
    assert readiness_config.signoff.enables_trading is False


def test_the_shipped_policy_defines_every_criterion_it_names(
    policy: ReadinessPolicy,
) -> None:
    assert policy.unknown_criteria() == ()
