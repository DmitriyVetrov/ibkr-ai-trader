"""The safety gates a cleanup must pass before a broker is constructed.

A pure function of captured state: settings, configuration, the reconciliation
that identified the targets, the fresh broker snapshot and the account the
connection resolved. No broker, no repository, no clock — the instant is passed
in — so a stored gate verdict can be re-derived and checked.

**Order matters, and the ordering is the design.** Every mode and guard check
runs *before* anything that could touch a connection, so a system in the wrong
mode never reaches the point of opening one. The claim these tests make is
therefore "the broker was never asked", which is much stronger than "the broker
refused" — a gate placed after the connection attempt would pass a test that
asserts a refusal, while still having dialled.

Nothing here is inferred from configuration alone. ``TRADING_MODE=PAPER`` in a
file is a statement of intent; the connected account proving it starts ``DU``
is evidence, and both are required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique

from trading_system.cleanup.models import CleanupTarget
from trading_system.domain.enums import ReconciliationRunStatus, TradingMode
from trading_system.infrastructure.settings import CleanupConfig, ExecutionConfig, Settings
from trading_system.reconciliation.models import ReconciliationResult

__all__ = [
    "GateOutcome",
    "GateVerdict",
    "evaluate_run_gates",
    "evaluate_target_gates",
]

#: Reconciliation outcomes in which a comparison genuinely happened. Everything
#: else — an unreadable broker, an unreadable ledger, a configuration failure —
#: produced no comparison at all, and a run that compared nothing cannot have
#: established that a holding is unaccounted for.
_COMPARISON_WAS_MADE = frozenset({ReconciliationRunStatus.MATCH, ReconciliationRunStatus.MISMATCH})


@unique
class GateOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """One named check and what it decided."""

    name: str
    outcome: GateOutcome
    detail: str

    @property
    def passed(self) -> bool:
        return self.outcome is GateOutcome.PASS

    def render(self) -> str:
        return f"{self.outcome.value:<4} {self.name}: {self.detail}"


def _gate(name: str, ok: bool, *, ok_detail: str, fail_detail: str) -> GateVerdict:
    return GateVerdict(
        name=name,
        outcome=GateOutcome.PASS if ok else GateOutcome.FAIL,
        detail=ok_detail if ok else fail_detail,
    )


def evaluate_run_gates(
    *,
    settings: Settings,
    cleanup: CleanupConfig,
    execution: ExecutionConfig,
    authorized: bool,
    dry_run: bool,
    result: ReconciliationResult,
    target_count: int,
    at: datetime,
    broker_account_id: str | None = None,
) -> tuple[GateVerdict, ...]:
    """Every gate that applies to the run as a whole.

    ``broker_account_id`` is the account **the broker itself reported** on this
    run's own observation, never the configured one — comparing the
    configuration against itself would prove nothing. ``None`` means no fresh
    observation was made, which is a failed gate rather than a skipped one.

    Returned in evaluation order, and *all* of them are evaluated even after
    one fails: an operator debugging a refusal needs the whole picture, and
    short-circuiting would hide a second problem behind the first. The caller
    submits nothing unless every verdict passes.
    """
    verdicts: list[GateVerdict] = [
        _gate(
            "TRADING_MODE_IS_PAPER",
            settings.trading_mode is TradingMode.PAPER,
            ok_detail="TRADING_MODE=PAPER",
            fail_detail=(
                f"TRADING_MODE={settings.trading_mode.value}. This operation submits orders in "
                f"PAPER and in nothing else, and no flag on this command changes that"
            ),
        ),
        _gate(
            "LIVE_TRADING_NOT_CONFIRMED",
            not settings.live_trading_confirmed,
            ok_detail="LIVE_TRADING_CONFIRMED=false",
            fail_detail=(
                "LIVE_TRADING_CONFIRMED is set. A live guard being active is ambiguous here, "
                "and this operation stops before constructing a broker rather than reasoning "
                "about which mode it is really in"
            ),
        ),
        _gate(
            "LIVE_CHECKLIST_NOT_SIGNED_OFF",
            not settings.live_readiness_checklist_signed_off,
            ok_detail="LIVE_READINESS_CHECKLIST_SIGNED_OFF=false",
            fail_detail=(
                "LIVE_READINESS_CHECKLIST_SIGNED_OFF is set. Same reasoning: an ambiguous live "
                "guard stops this before a connection exists"
            ),
        ),
        _gate(
            "CLEANUP_PAPER_ONLY",
            cleanup.paper_only and not cleanup.allow_live,
            ok_detail="cleanup.paper_only=true, cleanup.allow_live=false",
            fail_detail="cleanup configuration does not restrict this operation to PAPER",
        ),
        _gate(
            "EXECUTION_PAPER_ONLY",
            execution.paper_only and not execution.allow_live,
            ok_detail="execution.paper_only=true, execution.allow_live=false",
            fail_detail="execution configuration does not restrict submission to PAPER",
        ),
        _gate(
            "CLEANUP_ENABLED",
            cleanup.enabled,
            ok_detail="cleanup.enabled=true",
            fail_detail=(
                "cleanup.enabled is false in config/cleanup.yaml. This operation is switched "
                "off at the system level; it still shows what it would do"
            ),
        ),
        _gate(
            "EXECUTION_ENABLED",
            execution.enabled,
            ok_detail="execution.enabled=true",
            fail_detail=(
                "execution.enabled is false in config/execution.yaml. Order submission is "
                "switched off at the system level, for a cleanup exactly as for a trade"
            ),
        ),
        _gate(
            "EXPLICIT_AUTHORIZATION",
            authorized,
            ok_detail="--confirm was given",
            fail_detail=(
                "no cleanup authorisation was given. Listing the orphan holdings in an account "
                "is not permission to sell out of them: pass --confirm to authorise"
            ),
        ),
        _gate(
            "RECONCILIATION_USABLE",
            result.status in _COMPARISON_WAS_MADE,
            ok_detail=f"source reconciliation is {result.status.value}",
            fail_detail=(
                f"the source reconciliation is {result.status.value}: no comparison was made, "
                f"so its silence about a holding is not a statement that the holding is an "
                f"orphan. 'We could not look' and 'nothing accounts for this' are different "
                f"facts and only the second authorises anything"
            ),
        ),
        _gate(
            "RECONCILIATION_FRESH",
            _age_seconds(result, at) <= cleanup.max_reconciliation_age_seconds,
            ok_detail=(
                f"source reconciliation is {_age_seconds(result, at):.0f}s old "
                f"(limit {cleanup.max_reconciliation_age_seconds}s)"
            ),
            fail_detail=(
                f"the source reconciliation is {_age_seconds(result, at):.0f}s old, past the "
                f"{cleanup.max_reconciliation_age_seconds}s limit. An orphan list from earlier "
                f"describes an account that has since traded; run reconciliation again"
            ),
        ),
        _gate(
            "TARGET_COUNT_WITHIN_LIMIT",
            target_count <= cleanup.max_targets_per_run,
            ok_detail=f"{target_count} target(s), limit {cleanup.max_targets_per_run}",
            fail_detail=(
                f"{target_count} targets exceeds the {cleanup.max_targets_per_run} permitted in "
                f"one run. A ceiling is what stops a misconfiguration becoming a liquidation"
            ),
        ),
        _gate(
            "TARGETS_PRESENT",
            target_count > 0,
            ok_detail=f"{target_count} orphan holding(s) selected",
            fail_detail="no orphan holding is currently targetable; nothing to do",
        ),
    ]

    if not dry_run:
        # Only meaningful once a connection exists. A dry run never opens one,
        # so asserting anything about the connected account there would be a
        # claim about something that was never asked.
        verdicts.append(
            _gate(
                "FRESH_BROKER_OBSERVATION",
                broker_account_id is not None,
                ok_detail="this run read the broker itself",
                fail_detail=(
                    "this run made no broker observation of its own — a stored reconciliation "
                    "was named instead. Nothing then establishes that the holdings are still "
                    "there, that no order is working against them, or which account is "
                    "actually connected. Re-run without --reconciliation-id"
                ),
            )
        )
        verdicts.append(
            _gate(
                "BROKER_ACCOUNT_IS_PAPER",
                _is_paper_account(broker_account_id, execution.paper_account_prefixes),
                ok_detail=(
                    f"the connected account matches a paper prefix "
                    f"({', '.join(execution.paper_account_prefixes)})"
                ),
                fail_detail=(
                    "the connected account cannot be shown to be a paper account. Refusing "
                    "to submit rather than assuming; the account is evidence and the "
                    "configured mode is only a statement of intent"
                ),
            )
        )
        verdicts.append(
            _gate(
                "BROKER_ACCOUNT_MATCHES_EXPECTED",
                _matches_expected(broker_account_id, settings.ibkr_account_id),
                ok_detail=("the account the broker actually reported matches the configured one"),
                fail_detail=(
                    "the connected account is not the configured one. Closing holdings in an "
                    "account nobody named is exactly the mistake this refuses to make"
                ),
            )
        )

    return tuple(verdicts)


def evaluate_target_gates(
    *,
    target: CleanupTarget,
    cleanup: CleanupConfig,
    working_order_contract_ids: frozenset[int],
) -> tuple[GateVerdict, ...]:
    """Every gate that applies to one holding."""
    return (
        _gate(
            "IDENTIFIED_BY_CONTRACT_ID",
            target.key.startswith("cid:"),
            ok_detail=f"addressed by broker contract id {target.contract_id}",
            fail_detail="not addressed by a broker contract id",
        ),
        _gate(
            "HOLDING_IS_LONG",
            target.is_long or cleanup.allow_short_positions,
            ok_detail=f"long {target.quantity}",
            fail_detail=(
                f"the holding is {target.quantity}. Closing a short is a purchase whose cost is "
                f"unbounded above; it is reported and left exactly where it is"
            ),
        ),
        _gate(
            "NO_WORKING_BROKER_ORDER",
            target.contract_id not in working_order_contract_ids,
            ok_detail="no order is working at the broker for this contract",
            fail_detail=(
                f"an order is already working at the broker for contract {target.contract_id}. "
                f"Whether it is ours or not, a second one could sell this holding twice"
            ),
        ),
        _gate(
            "REFERENCE_PRICE_AVAILABLE",
            target.market_price is not None and target.market_price > 0,
            ok_detail=f"broker market price {target.market_price}",
            fail_detail=(
                "the broker reports no usable market price for this holding, so no limit price "
                "can be derived from one. It is not the average cost, not the strike and not a "
                "midpoint conjured from one side"
            ),
        ),
        _gate(
            "MULTIPLIER_REPORTED",
            target.multiplier is not None and target.multiplier >= 1,
            ok_detail=f"multiplier {target.multiplier}",
            fail_detail=(
                "the broker reports no contract multiplier. A standard US equity option is 100, "
                "and a system that assumed so would misprice the first one that is not"
            ),
        ),
    )


def _age_seconds(result: ReconciliationResult, at: datetime) -> float:
    return max((at - result.observed_at).total_seconds(), 0.0)


def _is_paper_account(account_id: str | None, prefixes: list[str]) -> bool:
    if not account_id or not prefixes:
        return False
    return any(account_id.upper().startswith(prefix.upper()) for prefix in prefixes)


def _matches_expected(account_id: str | None, expected: str | None) -> bool:
    """Both must be present and equal.

    An unset expectation is a fail, not a pass. "We did not configure which
    account" is not evidence that the connected one is right, and this is the
    gate that stops a cleanup running against whichever session happens to be
    open.
    """
    if not account_id or not expected:
        return False
    return account_id.strip().upper() == expected.strip().upper()
