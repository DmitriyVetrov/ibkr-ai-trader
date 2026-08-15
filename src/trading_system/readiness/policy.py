"""Readiness policy, resolved from configuration (Milestone 12).

The bridge between ``config/readiness.yaml`` and the evaluator. A
:class:`ReadinessPolicy` is a plain frozen value: which criteria block which
level, how long each kind of evidence stays usable, and which criteria are
bound to a revision rather than to a clock.

It exists so the evaluator can be a pure function of *two* values — evidence
and policy — rather than of evidence plus a configuration object it would have
to reach into. That matters for reproducibility: a stored assessment records
the policy version it was judged under, and a policy that changed underneath a
replay would silently change the verdict.

Nothing here reads a file. :meth:`ReadinessPolicy.of` takes an already-loaded
:class:`~trading_system.infrastructure.settings.ReadinessConfig`; loading is
the caller's job, and the caller is a service.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trading_system.domain.enums import ReadinessCriterionId, ReadinessLevel
from trading_system.infrastructure.settings import ReadinessConfig
from trading_system.readiness.criteria import READINESS_CRITERIA

__all__ = ["ReadinessPolicy"]


@dataclass(frozen=True, slots=True)
class ReadinessPolicy:
    """Which criteria block which level, and how evidence ages."""

    config_version: str
    paper_blocking: frozenset[ReadinessCriterionId]
    live_review_blocking: frozenset[ReadinessCriterionId]
    revision_bound: frozenset[ReadinessCriterionId]
    windows: dict[str, float] = field(default_factory=dict)

    @classmethod
    def of(cls, config: ReadinessConfig) -> ReadinessPolicy:
        """Resolve a policy from loaded configuration."""
        windows = config.freshness.windows
        return cls(
            config_version=config.config_version,
            paper_blocking=config.blocking_for_paper(),
            live_review_blocking=config.blocking_for_live_review(),
            revision_bound=frozenset(config.freshness.revision_bound),
            windows={
                "broker_seconds": windows.broker_seconds,
                "health_seconds": windows.health_seconds,
                "reconciliation_seconds": windows.reconciliation_seconds,
                "pnl_seconds": windows.pnl_seconds,
                "scheduler_seconds": windows.scheduler_seconds,
                "observability_seconds": windows.observability_seconds,
                "configuration_seconds": windows.configuration_seconds,
            },
        )

    def blocking_levels(self, criterion_id: ReadinessCriterionId) -> tuple[ReadinessLevel, ...]:
        """Which levels this criterion holds shut when unsatisfied.

        Returned in ascending order, so a rendered criterion reads
        ``READY_FOR_PAPER, READY_FOR_LIVE_REVIEW`` rather than in whatever
        order a set happened to iterate.
        """
        levels: list[ReadinessLevel] = []
        if criterion_id in self.paper_blocking:
            levels.append(ReadinessLevel.READY_FOR_PAPER)
        if criterion_id in self.live_review_blocking:
            levels.append(ReadinessLevel.READY_FOR_LIVE_REVIEW)
        return tuple(levels)

    def is_revision_bound(self, criterion_id: ReadinessCriterionId) -> bool:
        """Whether this criterion's evidence expires with the code, not the clock.

        A test result belongs to the commit it ran against. No amount of
        elapsed time makes it wrong while the code is unchanged, and no amount
        of freshness makes it right once the code has moved — which is why
        these two kinds of staleness cannot share one mechanism.
        """
        return criterion_id in self.revision_bound

    def window_seconds(self, window: str | None) -> float | None:
        """The freshness window named by a criterion, if it has one."""
        return None if window is None else self.windows.get(window)

    def unknown_criteria(self) -> tuple[ReadinessCriterionId, ...]:
        """Blocking criteria that the catalogue does not define.

        A configuration naming a criterion no definition covers would produce a
        gate that can never open, silently: the criterion would never be
        evaluated, so it would never pass, so the level it blocks would stay
        shut forever with nothing on screen explaining why. ``readiness
        validate`` reports this rather than letting it happen.
        """
        defined = {definition.criterion_id for definition in READINESS_CRITERIA}
        configured = self.paper_blocking | self.live_review_blocking
        return tuple(sorted(configured - defined, key=lambda c: c.value))
