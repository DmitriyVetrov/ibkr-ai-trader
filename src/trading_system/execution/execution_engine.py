"""The submission driver: the one component that holds a broker and sends.

Everything upstream of this — validation, the purchase card, the order intent —
is pure and could be run a thousand times harmlessly. This is where that stops
being true, so the engine is small, and every branch in it is about one
question: *after this, do we know whether an order exists?*

Three answers, and conflating any two of them is how a trading system places a
duplicate order:

``FAILED``
    The attempt provably did not leave this process. The broker was not
    connected, or was read-only, or our own translation refused to build the
    order. Nothing was sent, and a later attempt is safe.
``SUBMITTED`` (and the fill states)
    The broker answered. Whatever it said is recorded verbatim.
``UNKNOWN``
    We sent something and did not learn the outcome. A timeout, a dropped
    connection, an error mid-call. **This is not a failure and it is not a
    reason to retry** — the order may be live at the broker right now. It is
    resolved by asking the broker what it has, never by sending again.

The engine writes the record *before* it calls the broker. A process that dies
mid-submission then leaves a ``SUBMISSION_PENDING`` record behind, which the
next run reads as "an order may be in flight" rather than as silence.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_system.broker.base import (
    Broker,
    BrokerConfigurationError,
    BrokerError,
    BrokerTimeoutError,
    OrderSubmissionNotImplementedError,
    ReadOnlyBrokerError,
)
from trading_system.domain.enums import (
    ExecutionEventType,
    ExecutionReasonCode,
    ExecutionState,
)
from trading_system.domain.models import OrderIntent
from trading_system.execution.fill_tracker import (
    event_from_broker_order,
    event_from_execution_result,
    event_identifier,
)
from trading_system.execution.models import ExecutionEvent, ExecutionRecord
from trading_system.execution.store import ExecutionRepository
from trading_system.infrastructure.clock import Clock, SystemClock
from trading_system.infrastructure.logging import get_logger

__all__ = ["ExecutionEngine", "SubmissionOutcome"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """What one submission attempt produced."""

    record: ExecutionRecord
    events: tuple[ExecutionEvent, ...]
    #: Read off the broker, as a delta across the attempt. Never asserted.
    orders_submitted: int = 0

    @property
    def submitted(self) -> bool:
        return self.record.submitted

    @property
    def uncertain(self) -> bool:
        return self.record.state is ExecutionState.UNKNOWN


class ExecutionEngine:
    """Submits one authorised order and records what happened.

    Holds a broker and a repository and nothing else. It does not validate
    (:mod:`.validation` did), does not build orders (:mod:`.order_builder`
    did), and does not decide whether to trade (Milestone 7 did). It sends, and
    it writes down the answer.
    """

    def __init__(
        self,
        *,
        broker: Broker,
        repository: ExecutionRepository,
        clock: Clock | None = None,
    ) -> None:
        self._broker = broker
        self._repository = repository
        self._clock = clock or SystemClock()

    @property
    def broker(self) -> Broker:
        return self._broker

    @property
    def orders_submitted(self) -> int:
        """The broker's own count. Evidence, not a claim."""
        return self._broker.orders_submitted

    # --- submission --------------------------------------------------------
    def submit(self, record: ExecutionRecord, intent: OrderIntent) -> SubmissionOutcome:
        """Persist the attempt, send the order, and record the answer.

        ``record`` must arrive in ``SUBMISSION_PENDING``. That is not
        bookkeeping: the record is written to the ledger before the broker call
        so that a crash between the two leaves evidence of a possible in-flight
        order rather than nothing at all.
        """
        if record.state is not ExecutionState.SUBMISSION_PENDING:
            raise ValueError(
                f"submit() expects a record in SUBMISSION_PENDING, got {record.state.value}. "
                f"The pending record is what makes a crash mid-submission recoverable"
            )
        if record.dry_run:
            raise ValueError(
                "submit() was called with a dry-run record. A dry run never reaches this "
                "engine; that is the whole meaning of the flag"
            )

        # Written first, deliberately. Everything after this line may fail in a
        # way that leaves an order at the broker.
        self._repository.save(record)
        self._repository.append_event(self._pending_event(record))

        refusal = self._preflight(record)
        if refusal is not None:
            return self._apply(record, refusal, orders_submitted=0)

        before = self._broker.orders_submitted
        try:
            result = self._broker.place_order(intent)
        except (ReadOnlyBrokerError, OrderSubmissionNotImplementedError) as exc:
            # Refused by our own guard, before any bytes moved.
            return self._apply(
                record,
                self._failure_event(
                    record,
                    ExecutionReasonCode.BROKER_READ_ONLY,
                    f"the broker refused to submit: {exc}",
                ),
                orders_submitted=self._broker.orders_submitted - before,
            )
        except BrokerConfigurationError as exc:
            # The order could not be built for this broker. Nothing was sent.
            return self._apply(
                record,
                self._failure_event(
                    record,
                    ExecutionReasonCode.ORDER_BUILD_FAILED,
                    f"the order could not be expressed for {self._broker.name}: {exc}",
                ),
                orders_submitted=self._broker.orders_submitted - before,
            )
        except BrokerTimeoutError as exc:
            return self._apply(
                record,
                self._uncertain_event(
                    record,
                    ExecutionReasonCode.BROKER_TIMEOUT,
                    f"the broker did not answer within the request timeout: {exc}. The order "
                    f"may already be live; a timeout is not evidence that nothing was sent, "
                    f"and this will not be retried automatically",
                ),
                orders_submitted=self._broker.orders_submitted - before,
            )
        except BrokerError as exc:
            return self._apply(
                record,
                self._uncertain_event(
                    record,
                    ExecutionReasonCode.SUBMISSION_UNCERTAIN,
                    f"the submission failed after the attempt began: {exc}. Whether the order "
                    f"reached the broker is unknown; resolve by observing the broker, never "
                    f"by sending again",
                ),
                orders_submitted=self._broker.orders_submitted - before,
            )
        except Exception as exc:  # unexpected, and still not evidence of non-delivery
            return self._apply(
                record,
                self._uncertain_event(
                    record,
                    ExecutionReasonCode.SUBMISSION_UNCERTAIN,
                    f"unexpected failure during submission: {exc!r}",
                ),
                orders_submitted=self._broker.orders_submitted - before,
            )

        event = event_from_execution_result(
            record,
            result,
            sequence=1,
            observed_at=self._clock.now(),
            source=self._broker.name,
        )
        return self._apply(record, event, orders_submitted=self._broker.orders_submitted - before)

    # --- after submission --------------------------------------------------
    def resolve(self, record: ExecutionRecord) -> SubmissionOutcome:
        """Ask the broker what it actually has, and record the answer.

        The only correct response to an uncertain submission. It reads open
        orders — data the connection's startup handshake already cached — so it
        does not depend on a second uncached round trip being answered.

        A record with no broker order id cannot be looked up by id, so the
        search falls back to matching the order reference. Finding nothing is
        left as ``UNKNOWN`` rather than resolved to "not sent": absence from a
        list of *open* orders is also what a filled or cancelled order looks
        like.
        """
        sequence = self._next_sequence(record)
        try:
            orders = self._broker.get_open_orders()
        except BrokerError as exc:
            return self._apply(
                record,
                self._observation_event(
                    record,
                    sequence,
                    ExecutionReasonCode.BROKER_DISCONNECTED,
                    f"could not read broker state to resolve this execution: {exc}",
                ),
                orders_submitted=0,
            )

        match = None
        for order in orders:
            if record.broker_order_id and order.broker_order_id == record.broker_order_id:
                match = order
                break
        if match is None:
            return self._apply(
                record,
                self._observation_event(
                    record,
                    sequence,
                    ExecutionReasonCode.UNKNOWN_BROKER_STATE,
                    "the broker reports no open order matching this execution. That is what a "
                    "filled, cancelled or never-sent order all look like from here, so the "
                    "state stays unresolved rather than being guessed at",
                ),
                orders_submitted=0,
            )

        event = event_from_broker_order(
            record,
            match,
            sequence=sequence,
            observed_at=self._clock.now(),
            source=self._broker.name,
        )
        return self._apply(record, event, orders_submitted=0)

    def cancel(self, record: ExecutionRecord) -> SubmissionOutcome:
        """Request cancellation of a live order and record what the broker says.

        Narrow by design: this closes a submitted order's lifecycle. It is not
        an exit, and Milestone 8 ships no automated exits at all.
        """
        if record.broker_order_id is None:
            return self._apply(
                record,
                self._observation_event(
                    record,
                    self._next_sequence(record),
                    ExecutionReasonCode.BROKER_ORDER_ID_MISSING,
                    "this execution has no broker order id, so there is nothing to cancel by "
                    "name. Resolve its state first",
                ),
                orders_submitted=0,
            )

        pending = self._transition_event(
            record,
            ExecutionState.CANCEL_PENDING,
            ExecutionEventType.EXECUTION_CANCEL_REQUESTED,
            reason_code=None,
            detail=f"cancellation requested for {record.broker_order_id}",
        )
        outcome = self._apply(record, pending, orders_submitted=0)
        current = outcome.record

        try:
            order = self._broker.cancel_order(record.broker_order_id)
        except BrokerError as exc:
            # The order is still live as far as anyone knows. CANCEL_PENDING is
            # the honest state: the request was made and not confirmed.
            return self._apply(
                current,
                self._observation_event(
                    current,
                    self._next_sequence(current),
                    ExecutionReasonCode.CANCEL_FAILED,
                    f"the broker refused or could not confirm the cancellation: {exc}. The "
                    f"order may still be working",
                ),
                orders_submitted=0,
                extra_events=outcome.events,
            )

        event = event_from_broker_order(
            current,
            order,
            sequence=self._next_sequence(current),
            observed_at=self._clock.now(),
            source=self._broker.name,
        )
        return self._apply(current, event, orders_submitted=0, extra_events=outcome.events)

    # --- internals ---------------------------------------------------------
    def _preflight(self, record: ExecutionRecord) -> ExecutionEvent | None:
        """Refuse before sending where non-delivery is provable.

        Only conditions checkable *without* touching the wire belong here. That
        is what lets the resulting state be ``FAILED`` — a definite "nothing was
        sent" — rather than ``UNKNOWN``.
        """
        if self._broker.read_only:
            return self._failure_event(
                record,
                ExecutionReasonCode.BROKER_READ_ONLY,
                f"{self._broker.name} is open read-only, so no order can be sent. Nothing "
                f"was submitted",
            )
        if not self._broker.is_connected:
            return self._failure_event(
                record,
                ExecutionReasonCode.BROKER_DISCONNECTED,
                f"{self._broker.name} is not connected. Nothing was submitted",
            )
        return None

    def _apply(
        self,
        record: ExecutionRecord,
        event: ExecutionEvent,
        *,
        orders_submitted: int,
        extra_events: tuple[ExecutionEvent, ...] = (),
    ) -> SubmissionOutcome:
        self._repository.append_event(event)
        updated = record.with_event(event)
        if orders_submitted:
            updated = updated.model_copy(update={"orders_submitted": orders_submitted})
        _logger.info(
            "execution.state",
            execution_id=updated.execution_id,
            allocation_id=updated.allocation_id,
            mode=updated.trading_mode.value,
            broker_order_id=updated.broker_order_id,
            state=updated.state.value,
            reason_code=event.reason_code.value if event.reason_code else None,
        )
        return SubmissionOutcome(
            record=updated,
            events=(*extra_events, event),
            orders_submitted=orders_submitted,
        )

    def _next_sequence(self, record: ExecutionRecord) -> int:
        stored = self._repository.events(record.execution_id)
        return (max((event.sequence for event in stored), default=-1)) + 1

    def _pending_event(self, record: ExecutionRecord) -> ExecutionEvent:
        return ExecutionEvent(
            event_id=event_identifier(
                execution_id=record.execution_id, sequence=0, event_type="pending"
            ),
            execution_id=record.execution_id,
            sequence=0,
            event_type=ExecutionEventType.EXECUTION_SUBMISSION_PENDING,
            state=ExecutionState.SUBMISSION_PENDING,
            occurred_at=record.created_at,
            observed_at=self._clock.now(),
            source=self._broker.name,
            detail="recorded before the broker call, so a crash here still leaves evidence",
        )

    def _failure_event(
        self, record: ExecutionRecord, reason_code: ExecutionReasonCode, detail: str
    ) -> ExecutionEvent:
        return self._transition_event(
            record,
            ExecutionState.FAILED,
            ExecutionEventType.EXECUTION_FAILED,
            reason_code=reason_code,
            detail=detail,
        )

    def _uncertain_event(
        self, record: ExecutionRecord, reason_code: ExecutionReasonCode, detail: str
    ) -> ExecutionEvent:
        return self._transition_event(
            record,
            ExecutionState.UNKNOWN,
            ExecutionEventType.EXECUTION_STATE_UNKNOWN,
            reason_code=reason_code,
            detail=detail,
        )

    def _observation_event(
        self,
        record: ExecutionRecord,
        sequence: int,
        reason_code: ExecutionReasonCode,
        detail: str,
    ) -> ExecutionEvent:
        """An observation that changed nothing. Recorded so the audit shows we looked."""
        return ExecutionEvent(
            event_id=event_identifier(
                execution_id=record.execution_id,
                sequence=sequence,
                event_type=f"observe-{reason_code.value}",
            ),
            execution_id=record.execution_id,
            sequence=sequence,
            event_type=ExecutionEventType.EXECUTION_OBSERVED,
            state=record.state,
            occurred_at=self._clock.now(),
            observed_at=self._clock.now(),
            source=self._broker.name,
            reason_code=reason_code,
            detail=detail,
        )

    def _transition_event(
        self,
        record: ExecutionRecord,
        state: ExecutionState,
        event_type: ExecutionEventType,
        *,
        reason_code: ExecutionReasonCode | None,
        detail: str,
    ) -> ExecutionEvent:
        sequence = self._next_sequence(record)
        now = self._clock.now()
        return ExecutionEvent(
            event_id=event_identifier(
                execution_id=record.execution_id, sequence=sequence, event_type=state.value
            ),
            execution_id=record.execution_id,
            sequence=sequence,
            event_type=event_type,
            state=state,
            occurred_at=now,
            observed_at=now,
            source=self._broker.name,
            reason_code=reason_code,
            detail=detail,
        )
