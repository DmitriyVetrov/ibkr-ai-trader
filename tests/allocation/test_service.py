"""The allocation service, its ledger and its idempotency.

Brief sections 16, 20, 21, 22, 35, 37.2 and 37.6. The service is where the
milestone's *durable* claims live, and they are the ones that only fail later:

* a run is immutable and append-only, so an authorisation made on 10 August
  stays explainable in November;
* campaign state is **replayed from the ledger**, never carried as a running
  total, so it cannot drift;
* running twice over the same upstream artifacts does not reserve the capital
  twice;
* ``--dry-run`` reserves nothing and is never written to the ledger.

Everything here runs against temporary stores. No test in this file reaches a
broker, a model or the repository's own ``data/`` directory.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.allocation.service import AllocationService
from trading_system.allocation.store import AllocationStoreError, FilesystemAllocationRepository
from trading_system.domain.enums import (
    AllocationOutcome,
    AllocationRunStatus,
    ConfidenceLevel,
    ContractSelectionStatus,
    DecisionMethod,
    LegAction,
    MarketHypothesis,
    OptionRight,
    StrategyAction,
    StrategySelectionReason,
    StrategySelectionStatus,
    StrategyType,
    StrikeSelectionPolicy,
    TradingMode,
)
from trading_system.domain.models import SystemVersions
from trading_system.infrastructure.clock import FixedClock
from trading_system.infrastructure.settings import Settings
from trading_system.risk.models import AccountSnapshot
from trading_system.risk.store import FilesystemAccountSnapshotRepository
from trading_system.strategies.models import (
    ContractCostEstimate,
    ContractRunCounts,
    ContractSelectionResult,
    ContractSelectionRunResult,
    SelectedLeg,
    StrategyDecisionRecord,
    StrategyRunCounts,
    StrategyRunResult,
)
from trading_system.strategies.store import (
    FilesystemContractSelectionRepository,
    FilesystemStrategyRepository,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
EXPIRATION = date(2026, 8, 31)
VERSIONS = SystemVersions(application_version="0.1.0", config_version="test")


class _Default:
    """Distinguishes "use the default account" from an explicit ``None``.

    ``None`` means *store no account snapshot at all*, which is a case worth
    testing; a plain default of ``None`` could not express both.
    """


DEFAULT = _Default()


def _selection(
    symbol: str,
    *,
    debit: str = "605.00",
    run_id: str = "contract-run-0001",
    status: ContractSelectionStatus = ContractSelectionStatus.SUCCESS,
) -> ContractSelectionResult:
    """One successful Milestone 6 selection, priced and quoted."""
    legs = (
        [
            SelectedLeg(
                leg_index=0,
                action=LegAction.BUY,
                right=OptionRight.CALL,
                underlying=symbol,
                expiration=EXPIRATION,
                dte=21,
                strike=Decimal("180.00"),
                multiplier=100,
                trading_class=symbol,
                contract_id=abs(hash(symbol)) % 10_000_000,
                exchange="SMART",
                currency="EUR",
                strike_policy=StrikeSelectionPolicy.TARGET_DELTA,
                selection_reason="TARGET_DELTA: closest to the configured target",
                bid=Decimal("5.95"),
                ask=Decimal("6.05"),
                delta=Decimal("0.60"),
                chain_snapshot_id=f"snap-chain-{symbol.lower()}",
                quote_snapshot_id=f"snap-quotes-{symbol.lower()}",
                quote_as_of=NOW,
            )
        ]
        if status is ContractSelectionStatus.SUCCESS
        else []
    )
    return ContractSelectionResult(
        selection_id=f"contract-{symbol}-0001",
        run_id=run_id,
        symbol=symbol,
        as_of=NOW,
        generated_at=NOW,
        selection_status=status,
        strategy=StrategyType.LONG_CALL,
        strategy_version="1.0.0",
        strategy_run_id="strategy-run-0001",
        strategy_decision_id=f"strategy-{symbol}-0001",
        research_report_id=f"research-{symbol}-0001",
        legs=legs,
        expiration=EXPIRATION if legs else None,
        dte=21 if legs else None,
        cost=(
            ContractCostEstimate(
                available=True,
                currency="EUR",
                estimated_debit=Decimal(debit),
                estimated_mid_debit=Decimal(debit),
                max_leg_spread_pct=1.67,
            )
            if legs
            else ContractCostEstimate(available=False, unavailable_reason="no contract selected")
        ),
        selection_policy_version="1.0.0",
        input_snapshot_ids=[f"snap-chain-{symbol.lower()}", f"snap-quotes-{symbol.lower()}"],
        versions=VERSIONS,
    )


def _contract_run(*selections: ContractSelectionResult) -> ContractSelectionRunResult:
    return ContractSelectionRunResult(
        run_id="contract-run-0001",
        as_of=NOW,
        generated_at=NOW,
        status=ContractSelectionStatus.SUCCESS,
        strategy_run_id="strategy-run-0001",
        research_run_id="research-run-0001",
        selection_policy_version="1.0.0",
        selections=list(selections),
        counts=ContractRunCounts(decisions_considered=len(selections), selected=len(selections)),
        versions=VERSIONS,
    )


def _strategy_run(*symbols: str) -> StrategyRunResult:
    """The Milestone 6 decisions the selections descend from.

    Present because the real pipeline always has one, and because the score
    reads its confidence bands from here. Without it every candidate scores as
    though nothing were known about it — which is honest, and which the
    ``no strategy run`` test below asserts directly.
    """
    decisions = [
        StrategyDecisionRecord(
            decision_id=f"strategy-{symbol}-0001",
            run_id="strategy-run-0001",
            symbol=symbol,
            as_of=NOW,
            generated_at=NOW,
            status=StrategySelectionStatus.SUCCESS,
            action=StrategyAction.BUY,
            selected_strategy=StrategyType.LONG_CALL,
            strategy_version="1.0.0",
            decision_method=DecisionMethod.AI_SELECTED,
            confidence=ConfidenceLevel.MEDIUM,
            reasons=[StrategySelectionReason.HYPOTHESIS_MATCH],
            rationale="the hypothesis matches what this strategy expresses",
            hypothesis=MarketHypothesis.B,
            research_confidence=ConfidenceLevel.MEDIUM,
            research_horizon_days=21,
            research_report_id=f"research-{symbol}-0001",
            research_run_id="research-run-0001",
            eligible_strategies=[StrategyType.LONG_CALL],
            versions=VERSIONS,
        )
        for symbol in symbols
    ]
    return StrategyRunResult(
        run_id="strategy-run-0001",
        as_of=NOW,
        generated_at=NOW,
        status=StrategySelectionStatus.SUCCESS,
        research_run_id="research-run-0001",
        decisions=decisions,
        counts=StrategyRunCounts(researched_assets=len(symbols), proposed=len(symbols)),
        versions=VERSIONS,
    )


def _account(**overrides) -> AccountSnapshot:
    fields = {
        "snapshot_id": "account-20260810T143000Z-abc",
        "as_of": NOW,
        "captured_at": NOW,
        "broker": "SIMULATOR",
        "account_id": "DU0000000",
        "currency": "EUR",
        "trading_mode": TradingMode.PAPER,
        "cash": Decimal("100000.00"),
        "buying_power": Decimal("400000.00"),
        "available_funds": Decimal("98000.00"),
        "simulated": True,
    }
    fields.update(overrides)
    return AccountSnapshot(**fields)


@pytest.fixture
def service(tmp_path: Path, system_config):
    """A service wired to temporary stores, with one priced NVDA candidate."""

    def _build(
        *selections: ContractSelectionResult,
        account: AccountSnapshot | _Default | None = DEFAULT,
        with_strategy_run: bool = True,
    ) -> AllocationService:
        chosen = selections or (_selection("NVDA"),)
        contracts = FilesystemContractSelectionRepository(tmp_path / "contracts")
        strategies = FilesystemStrategyRepository(tmp_path / "strategy")
        accounts = FilesystemAccountSnapshotRepository(tmp_path / "accounts")

        contracts.save(_contract_run(*chosen))
        if with_strategy_run:
            strategies.save(_strategy_run(*(s.symbol for s in chosen)))
        if account is not None:
            accounts.save(_account() if isinstance(account, _Default) else account)

        return AllocationService(
            settings=Settings(_env_file=None),
            config=system_config,
            clock=FixedClock(NOW),
            strategy_repository=strategies,
            contract_repository=contracts,
            allocation_repository=FilesystemAllocationRepository(tmp_path / "allocation"),
            account_repository=accounts,
            root=tmp_path,
        )

    return _build


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_a_run_authorises_capital_and_stores_it(service):
    run = service().run()

    assert run.result.status is AllocationRunStatus.SUCCESS
    assert run.stored is True
    allocation = run.result.allocation("NVDA")
    assert allocation is not None
    assert allocation.outcome is AllocationOutcome.APPROVED
    assert allocation.quantity >= 1
    assert allocation.capital_committed == allocation.unit_cost * allocation.quantity


def test_the_run_is_anchored_at_the_contract_runs_instant(service):
    """Allocating a stale selection against today's quotes would size a
    position nobody proposed."""
    run = service().run()

    assert run.result.as_of == NOW


def test_the_allocation_names_every_upstream_artifact(service):
    allocation = service().run().result.allocation("NVDA")

    assert allocation is not None
    assert allocation.contract_selection_id == "contract-NVDA-0001"
    assert allocation.contract_run_id == "contract-run-0001"
    assert allocation.strategy_decision_id == "strategy-NVDA-0001"
    assert allocation.research_report_id == "research-NVDA-0001"
    assert allocation.account_snapshot_id == "account-20260810T143000Z-abc"
    assert allocation.campaign_snapshot_as_of == NOW


def test_the_campaign_accounting_balances(service):
    result = service().run().result

    assert (
        result.allocated_before + result.allocated_this_run + result.available_after
        == result.budget - result.reserve
    )


def test_the_run_records_the_state_it_decided_against(service):
    """A decision at T1 must stay explainable when the account has moved on."""
    result = service().run().result

    assert result.campaign_before.as_of == NOW
    assert result.campaign_before.budget == Decimal("5000")
    assert result.account_snapshot_id is not None


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_running_twice_does_not_reserve_the_capital_twice(service, tmp_path):
    built = service()

    first = built.run()
    second = built.run()

    assert first.result.allocated_this_run > Decimal("0")
    assert second.result.allocated_this_run == Decimal("0")
    allocation = second.result.allocation("NVDA")
    assert allocation is not None
    assert allocation.outcome is AllocationOutcome.ALREADY_ALLOCATED

    repository = FilesystemAllocationRepository(tmp_path / "allocation")
    committed = sum(
        (
            a.capital_committed
            for run in repository.all_runs()
            for a in run.allocations
            if a.approved
        ),
        Decimal("0"),
    )
    assert committed == first.result.allocated_this_run


def test_the_second_run_sees_the_reduced_campaign(service):
    built = service()

    first = built.run()
    second = built.run()

    assert second.result.allocated_before == first.result.allocated_this_run
    assert second.result.available_after == first.result.available_after


def test_an_opportunity_id_is_stable_across_runs(service):
    built = service()

    first = built.run().result.allocation("NVDA")
    second = built.run().result.allocation("NVDA")

    assert first is not None and second is not None
    assert first.opportunity_id == second.opportunity_id


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
def test_a_dry_run_persists_nothing(service, tmp_path):
    run = service().run(dry_run=True)

    assert run.stored is False
    assert run.result.dry_run is True
    assert FilesystemAllocationRepository(tmp_path / "allocation").history() == []


def test_a_dry_run_reserves_no_capital(service):
    built = service()

    built.run(dry_run=True)
    built.run(dry_run=True)
    real = built.run()

    assert real.result.allocated_before == Decimal("0")
    allocation = real.result.allocation("NVDA")
    assert allocation is not None
    assert allocation.outcome is AllocationOutcome.APPROVED


def test_every_allocation_in_a_dry_run_is_marked(service):
    """Marking only the run would let one authorisation be lifted out of it."""
    run = service().run(dry_run=True)

    assert all(a.dry_run for a in run.result.allocations)


def test_the_ledger_refuses_a_dry_run(tmp_path, service):
    run = service().run(dry_run=True)
    repository = FilesystemAllocationRepository(tmp_path / "allocation")

    with pytest.raises(AllocationStoreError, match="refusing to store dry run"):
        repository.save(run.result)


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------
def test_no_contract_run_fails_closed(tmp_path, system_config):
    built = AllocationService(
        settings=Settings(_env_file=None),
        config=system_config,
        clock=FixedClock(NOW),
        contract_repository=FilesystemContractSelectionRepository(tmp_path / "contracts"),
        allocation_repository=FilesystemAllocationRepository(tmp_path / "allocation"),
        account_repository=FilesystemAccountSnapshotRepository(tmp_path / "accounts"),
        root=tmp_path,
    )

    run = built.run()

    assert run.result.status is AllocationRunStatus.NO_CONTRACT_RUN
    assert run.result.allocations == []
    assert "contract select" in (run.result.status_detail or "")


def test_a_missing_account_snapshot_fails_closed(service):
    run = service(_selection("NVDA"), account=None).run()

    assert run.result.status is AllocationRunStatus.ACCOUNT_SNAPSHOT_UNAVAILABLE
    assert run.result.allocations == []
    assert "risk capture-account" in (run.result.status_detail or "")


def test_a_contract_run_that_selected_nothing_yields_no_candidates(service):
    run = service(_selection("NVDA", status=ContractSelectionStatus.NO_VALID_CONTRACT)).run()

    assert run.result.status is AllocationRunStatus.NO_CANDIDATES
    assert "considered outcome, not a failure" in (run.result.status_detail or "")


def test_a_symbol_the_contract_run_did_not_cover_is_refused(service):
    """This stage allocates against a selection; it never makes one."""
    run = service().run(symbols=["TSLA"])

    assert run.result.status is AllocationRunStatus.CONFIGURATION_ERROR
    assert "did not cover TSLA" in (run.result.status_detail or "")


def test_without_a_strategy_run_nothing_is_assumed_about_the_candidate(service):
    """An absent confidence band scores as the least favourable, not the best.

    A contract selection whose strategy decision cannot be read is not
    evidence of a good opportunity, so it scores as though nothing were known
    and falls below the floor. The score components record exactly that, so
    the reason is visible rather than mysterious.
    """
    run = service(_selection("NVDA"), with_strategy_run=False).run()

    allocation = run.result.allocation("NVDA")
    assert allocation is not None
    assert allocation.outcome is AllocationOutcome.REJECTED
    assert allocation.opportunity_score.research_confidence == 40.0
    assert allocation.opportunity_score.strategy_confidence == 40.0


def test_a_run_that_authorises_nothing_is_not_a_failure(service):
    """NO_ALLOCATION is the ordinary answer when the campaign is committed."""
    built = service()
    built.run()

    second = built.run()

    assert second.result.status is AllocationRunStatus.NO_ALLOCATION
    assert second.result.allocated_this_run == Decimal("0")


# ---------------------------------------------------------------------------
# Immutability and history
# ---------------------------------------------------------------------------
def test_a_stored_run_survives_a_round_trip(service, tmp_path):
    stored = service().run().result
    repository = FilesystemAllocationRepository(tmp_path / "allocation")

    loaded = repository.get(stored.run_id)

    assert loaded is not None
    assert loaded.model_dump(mode="json") == stored.model_dump(mode="json")


def test_a_stored_run_is_immutable(service, tmp_path):
    stored = service().run().result
    repository = FilesystemAllocationRepository(tmp_path / "allocation")
    forged = stored.model_copy(update={"status_detail": "edited after the fact"})

    with pytest.raises(AllocationStoreError, match="immutable"):
        repository.save(forged)


def test_a_later_run_appends_rather_than_replacing(service, tmp_path):
    built = service()
    built.run()
    built.run()

    repository = FilesystemAllocationRepository(tmp_path / "allocation")

    assert len(repository.history()) == 2
    assert len(repository.all_runs()) == 2


def test_a_historical_allocation_is_unchanged_by_a_later_account(service, tmp_path):
    """A later balance never edits an earlier authorisation."""
    built = service()
    first = built.run().result
    accounts = FilesystemAccountSnapshotRepository(tmp_path / "accounts")
    accounts.save(
        _account(
            snapshot_id="account-20260810T160000Z-later",
            as_of=NOW + timedelta(hours=1),
            captured_at=NOW + timedelta(hours=1),
            cash=Decimal("1.00"),
            buying_power=Decimal("1.00"),
            available_funds=Decimal("1.00"),
        )
    )
    built.run()

    reloaded = FilesystemAllocationRepository(tmp_path / "allocation").get(first.run_id)

    assert reloaded is not None
    assert reloaded.model_dump(mode="json") == first.model_dump(mode="json")


def test_the_symbol_index_records_every_decision(service, tmp_path):
    service().run()

    entries = FilesystemAllocationRepository(tmp_path / "allocation").symbol_history("NVDA")

    assert len(entries) == 1
    assert entries[0].outcome == "APPROVED"
    assert entries[0].symbol == "NVDA"


def test_campaign_state_is_replayed_from_the_ledger(service, tmp_path):
    """Not a running total: a second copy of the truth would drift."""
    built = service()
    built.run()

    campaign = built.campaign_snapshot(NOW)

    assert campaign.position_count == 1
    assert campaign.allocated == Decimal("1210.00")
    assert campaign.available == Decimal("4000.00") - Decimal("1210.00")


def test_a_replay_at_an_earlier_instant_sees_no_commitments(service):
    built = service()
    built.run()

    earlier = built.campaign_snapshot(NOW - timedelta(days=1))

    assert earlier.position_count == 0
    assert earlier.allocated == Decimal("0")


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------
def test_two_services_over_the_same_inputs_agree(tmp_path, system_config, service):
    first = service().run(dry_run=True).result
    second = service().run(dry_run=True).result

    assert first.run_id == second.run_id
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_the_run_id_changes_with_the_account_snapshot(service, tmp_path):
    """The same candidates against a different balance are a different decision."""
    first = service().run(dry_run=True).result
    other = service(
        _selection("NVDA"),
        account=_account(
            snapshot_id="account-other", cash=Decimal("7.00"), available_funds=Decimal("7.00")
        ),
    )

    assert other.run(dry_run=True).result.run_id != first.run_id


def test_the_versions_of_every_policy_are_stamped(service, system_config):
    result = service().run().result

    assert result.versions.config_version == system_config.application.config_version
    assert result.versions.data_source_versions["risk"] == system_config.risk.config_version
    assert result.policy_version == system_config.campaign.allocation.policy_version
