"""Turn what the broker reported into a stored position snapshot.

Pure functions over the Milestone 2 domain models. Nothing here opens a
connection, and nothing here decides anything: it translates
:class:`~trading_system.domain.models.BrokerPosition` into
:class:`~trading_system.positions.models.ObservedPosition` and assembles the
immutable snapshot around it.

Three rules, each a way a position ledger starts lying:

* **A failed read is not an empty account.** :func:`unavailable_snapshot` is a
  separate constructor from :func:`build_position_snapshot`, and the model
  refuses to let either wear the other's shape. Reconciling an unreachable
  broker against the internal ledger would report every position the system
  believes in as missing and every real holding as gone.
* **Nothing is filled in.** A market value the broker did not report is
  ``None``. There is no code path here that multiplies a quantity by a
  reference price and calls the result a market value.
* **Identity comes from the broker.** The contract id is used wherever there is
  one; where there is not, the fallback key is used *and recorded as a
  fallback*, because a weaker identity should be visible.

The content digest deliberately covers the *holdings* — instrument, quantity,
average cost — and not their valuation. A snapshot answers "what does this
account hold"; re-reading the same holdings a minute later at a different mark
is a re-observation of the same account state, not a second one. That is the
same choice :func:`trading_system.risk.models.build_account_snapshot_payload`
makes, for the same reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trading_system.data.hashing import stable_hash
from trading_system.domain.enums import (
    AcquisitionProvenance,
    BrokerReadStatus,
    TradingMode,
)
from trading_system.domain.models import BrokerPosition
from trading_system.positions.models import (
    BrokerPositionSnapshot,
    ObservedPosition,
    contract_key,
    mask_account,
    position_identifier,
    position_snapshot_identifier,
)

__all__ = [
    "build_position_snapshot",
    "snapshot_payload",
    "to_observed_position",
    "unavailable_snapshot",
]

#: Broker names whose data is synthetic. Recorded on every artifact so a
#: simulated holding can never be read as a real one.
_SIMULATED_SOURCES = frozenset({"SIMULATOR", "SIMULATED"})


def to_observed_position(
    position: BrokerPosition,
    *,
    observed_at: datetime,
    account_reference: str | None = None,
    mask_visible: int = 4,
    provenance: AcquisitionProvenance = AcquisitionProvenance.UNKNOWN,
) -> ObservedPosition:
    """Normalise one broker position. Adds nothing the broker did not say."""
    reference = account_reference or mask_account(position.account_id, visible=mask_visible)
    key = contract_key(
        contract_id=position.contract_id,
        symbol=position.symbol,
        security_type=position.security_type,
        expiration=position.expiration,
        strike=position.strike,
        right=position.right,
        currency=position.currency,
    )
    return ObservedPosition(
        position_id=position_identifier(account_reference=reference, key=key),
        account_reference=reference,
        key=key,
        as_of=position.as_of,
        observed_at=observed_at,
        underlying=position.symbol.strip().upper(),
        asset_class=position.security_type,
        symbol=position.symbol,
        contract_id=position.contract_id,
        local_symbol=position.local_symbol,
        currency=position.currency,
        multiplier=position.multiplier,
        expiration=position.expiration,
        strike=position.strike,
        right=position.right,
        quantity=position.quantity,
        average_cost=position.average_cost,
        market_price=position.market_price,
        market_value=position.market_value,
        unrealized_pnl=position.unrealized_pnl,
        realized_pnl=position.realized_pnl,
        broker_source=position.source,
        broker_timestamp=position.as_of,
        provenance=provenance,
        identified_by_contract_id=key.startswith("cid:"),
        simulated=position.source.upper() in _SIMULATED_SOURCES,
    )


def snapshot_payload(positions: Sequence[ObservedPosition]) -> list[str]:
    """The content a snapshot's identity is derived from.

    Holdings, not valuations: instrument, quantity, average cost and currency.
    Sorted, so two readings of one account in whatever order the broker
    happened to return them produce the same digest.
    """
    return sorted(
        f"{position.key}|{position.quantity}|{position.average_cost}|"
        f"{position.currency}|{position.multiplier}"
        for position in positions
    )


def build_position_snapshot(
    positions: Sequence[BrokerPosition],
    *,
    broker: str,
    account_id: str | None,
    trading_mode: TradingMode,
    as_of: datetime,
    observed_at: datetime,
    mask_visible: int = 4,
    orders_submitted: int = 0,
    read_only: bool = True,
    provenance_by_key: dict[str, AcquisitionProvenance] | None = None,
) -> BrokerPositionSnapshot:
    """Assemble an immutable snapshot from what the broker actually returned.

    An empty ``positions`` list produces a snapshot with status ``EMPTY``,
    which is a *valid answer about the account*. A caller that could not read
    the broker must call :func:`unavailable_snapshot` instead — the difference
    is the whole point, and the models refuse to blur it.

    ``orders_submitted`` is read off the broker's own counter and stored. It is
    structurally zero because capturing positions reads and does nothing else;
    recording the counter rather than the constant is what makes that evidence.
    """
    reference = mask_account(account_id, visible=mask_visible)
    provenance = provenance_by_key or {}

    observed = []
    for position in positions:
        record = to_observed_position(
            position,
            observed_at=observed_at,
            account_reference=reference,
            mask_visible=mask_visible,
        )
        stamped = provenance.get(record.key)
        observed.append(
            record if stamped is None else record.model_copy(update={"provenance": stamped})
        )
    observed.sort(key=lambda position: (position.underlying, position.key))

    digest = stable_hash(snapshot_payload(observed))
    return BrokerPositionSnapshot(
        snapshot_id=position_snapshot_identifier(
            broker=broker,
            account_reference=reference,
            as_of=as_of,
            payload_digest=digest,
        ),
        account_reference=reference,
        broker=broker,
        trading_mode=trading_mode,
        as_of=as_of,
        observed_at=observed_at,
        read_status=BrokerReadStatus.OK if observed else BrokerReadStatus.EMPTY,
        positions=observed,
        content_hash=digest,
        broker_timestamp=min((p.as_of for p in positions), default=None),
        orders_submitted=orders_submitted,
        read_only=read_only,
        simulated=broker.upper() in _SIMULATED_SOURCES,
        detail=(
            None
            if observed
            else "the broker answered and reported no positions; this is a fact about the "
            "account, not a failed read"
        ),
    )


def unavailable_snapshot(
    *,
    broker: str,
    account_id: str | None,
    trading_mode: TradingMode,
    as_of: datetime,
    observed_at: datetime,
    status: BrokerReadStatus,
    detail: str,
    mask_visible: int = 4,
) -> BrokerPositionSnapshot:
    """Record that broker state could not be read.

    Deliberately a different function from :func:`build_position_snapshot`, so
    "we could not look" can never be produced by accident from the same call
    that produces "we looked and there was nothing". The resulting snapshot
    carries no positions, says why, and reports ``usable == False``; every
    consumer that reconciles against it is required to check.
    """
    if status.usable:
        raise ValueError(
            f"unavailable_snapshot was given {status.value}, which claims the broker answered. "
            f"Use build_position_snapshot for an answer, however empty"
        )
    reference = mask_account(account_id, visible=mask_visible)
    digest = stable_hash([status.value, detail])
    return BrokerPositionSnapshot(
        snapshot_id=position_snapshot_identifier(
            broker=broker,
            account_reference=reference,
            as_of=as_of,
            payload_digest=digest,
        ),
        account_reference=reference,
        broker=broker,
        trading_mode=trading_mode,
        as_of=as_of,
        observed_at=observed_at,
        read_status=status,
        positions=[],
        content_hash=digest,
        orders_submitted=0,
        read_only=True,
        simulated=broker.upper() in _SIMULATED_SOURCES,
        detail=detail,
    )
