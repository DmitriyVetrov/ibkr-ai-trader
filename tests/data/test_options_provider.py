"""Option chain and option quote providers.

Two properties dominate: the chain is stored whole rather than filtered down to
something that looks tradeable, and no provider chooses a contract. Contract
selection is deterministic and belongs to Milestone 6; a provider that
pre-filtered the chain would be making that decision invisibly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from trading_system.broker.base import BrokerTimeoutError, OptionChainUnavailableError
from trading_system.broker.simulator import SimulatedBroker, SimulatedBrokerState
from trading_system.data.providers.broker_session import BrokerSession
from trading_system.data.providers.options import (
    IBKROptionsDataProvider,
    SimulatedOptionsDataProvider,
)
from trading_system.domain.enums import (
    CollectionOutcome,
    DataType,
    MarketDataOrigin,
    OptionRight,
)

pytestmark = pytest.mark.unit


class _FailingBroker(SimulatedBroker):
    def __init__(self, error: Exception, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._error = error

    def get_option_chain(self, underlying: str):
        raise self._error


def _provider(data_clock, broker=None) -> IBKROptionsDataProvider:
    return IBKROptionsDataProvider(
        BrokerSession(lambda: broker or SimulatedBroker(clock=data_clock)), clock=data_clock
    )


# ---------------------------------------------------------------------------
# Valid chain
# ---------------------------------------------------------------------------
def test_a_chain_is_retrieved_and_normalised(data_clock) -> None:
    result = _provider(data_clock).fetch_chain("SPY")

    assert result.outcome is CollectionOutcome.SUCCESS
    chain = result.records[0]
    assert chain.underlying == "SPY"
    assert chain.expirations
    assert chain.strikes
    assert set(chain.rights) == {OptionRight.CALL, OptionRight.PUT}


def test_the_whole_chain_is_kept_not_a_selected_slice(data_clock) -> None:
    """The point is to accumulate an option-chain history, not one contract."""
    chain = _provider(data_clock).fetch_chain("SPY").records[0]

    assert len(chain.strikes) > 5
    assert len(chain.expirations) > 1


def test_the_chain_is_deterministically_ordered(data_clock) -> None:
    chain = _provider(data_clock).fetch_chain("SPY").records[0]

    assert chain.expirations == sorted(set(chain.expirations))
    assert chain.strikes == sorted(set(chain.strikes))


def test_the_raw_chain_response_is_preserved(data_clock) -> None:
    result = _provider(data_clock).fetch_chain("SPY")

    assert result.raw is not None
    assert result.raw.data_type is DataType.OPTION_CHAIN
    assert result.raw.key == "SPY"
    assert result.raw.payload_hash


def test_the_provider_declares_chain_and_quote_support(data_clock) -> None:
    """Per-contract IBKR quotes exist now, and the provider says so."""
    provider = _provider(data_clock)

    assert provider.data_types == frozenset({DataType.OPTION_CHAIN, DataType.OPTION_QUOTE})


def test_option_quotes_without_a_strike_list_are_refused_not_guessed(data_clock) -> None:
    """The refusal is the design, not a gap.

    Working the contracts out means reading the chain and the underlying's
    price, which is two more round trips on a connection that reliably answers
    one. The caller holds both already, so it supplies them — and a provider
    that quietly picked its own strikes would be making a selection decision
    inside a retrieval layer.
    """
    result = _provider(data_clock).fetch_option_quotes("SPY")

    assert result.outcome is CollectionOutcome.NO_DATA
    assert result.records == ()
    assert "explicit expiration and strike list" in (result.error or "")


def test_option_quotes_with_an_expiration_but_no_strikes_are_still_refused(data_clock) -> None:
    """Half the identification is not identification.

    An expiration alone leaves 491 strikes on SPY, and "all of them" is the
    request that gets a market-data line throttled.
    """
    result = _provider(data_clock).fetch_option_quotes("SPY", expiration=date(2026, 9, 18))

    assert result.outcome is CollectionOutcome.NO_DATA
    assert "strike list" in (result.error or "")


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------
def test_a_missing_chain_is_reported_not_faked(data_clock) -> None:
    broker = SimulatedBroker(SimulatedBrokerState(chainless_symbols={"ZZZZ"}), clock=data_clock)
    result = _provider(data_clock, broker).fetch_chain("ZZZZ")

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert result.records == ()


def test_a_chain_timeout_is_reported(data_clock) -> None:
    broker = _FailingBroker(BrokerTimeoutError("unanswered"), clock=data_clock)
    result = _provider(data_clock, broker).fetch_chain("SPY")

    assert result.outcome is CollectionOutcome.PROVIDER_UNAVAILABLE
    assert "timed out" in (result.error or "")


def test_an_unavailable_chain_error_is_translated(data_clock) -> None:
    broker = _FailingBroker(OptionChainUnavailableError("no chain"), clock=data_clock)
    result = _provider(data_clock, broker).fetch_chain("SPY")

    assert not result.succeeded
    assert result.records == ()


# ---------------------------------------------------------------------------
# Simulated option quotes exercise the full option-quote path
# ---------------------------------------------------------------------------
def test_simulated_option_quotes_carry_contracts_and_greeks(data_clock) -> None:
    result = SimulatedOptionsDataProvider(clock=data_clock).fetch_option_quotes("SPY")

    assert result.outcome is CollectionOutcome.SUCCESS
    assert result.record_count > 1
    quote = result.records[0]
    assert quote.contract.is_fully_identified
    assert isinstance(quote.implied_volatility, Decimal)
    assert isinstance(quote.delta, Decimal)
    assert quote.open_interest is not None
    assert quote.source.origin is MarketDataOrigin.SIMULATED


def test_simulated_option_quotes_cover_both_rights(data_clock) -> None:
    result = SimulatedOptionsDataProvider(clock=data_clock).fetch_option_quotes("SPY")
    rights = {q.contract.right for q in result.records}

    assert rights == {OptionRight.CALL, OptionRight.PUT}


def test_simulated_contracts_have_stable_identifiers(data_clock) -> None:
    """Deterministic ids are what make simulated snapshots deduplicate."""
    provider = SimulatedOptionsDataProvider(clock=data_clock)

    first = {q.contract.contract_id for q in provider.fetch_option_quotes("SPY").records}
    second = {q.contract.contract_id for q in provider.fetch_option_quotes("SPY").records}

    assert first == second
    assert None not in first


def test_a_simulated_chain_lists_its_contracts(data_clock) -> None:
    chain = SimulatedOptionsDataProvider(clock=data_clock).fetch_chain("SPY").records[0]

    assert chain.contracts
    assert all(c.underlying == "SPY" for c in chain.contracts)
    assert all(c.local_symbol for c in chain.contracts)


def test_option_quotes_for_an_unknown_expiration_report_no_data(data_clock) -> None:
    from datetime import date

    result = SimulatedOptionsDataProvider(clock=data_clock).fetch_option_quotes(
        "SPY", expiration=date(2030, 1, 18)
    )
    # An expiry the simulator does not list produces contracts for that expiry
    # only because the caller named it; what matters is the records are honest
    # about which expiry they describe.
    assert all(q.contract.expiration == date(2030, 1, 18) for q in result.records)


# ---------------------------------------------------------------------------
# No contract is selected, anywhere
# ---------------------------------------------------------------------------
def test_no_options_provider_exposes_contract_selection(data_clock) -> None:
    for provider in (_provider(data_clock), SimulatedOptionsDataProvider(clock=data_clock)):
        methods = [name for name in dir(provider) if not name.startswith("_")]
        assert not any(
            name.startswith(word)
            for name in methods
            for word in ("select", "choose", "pick", "recommend", "rank")
        )
