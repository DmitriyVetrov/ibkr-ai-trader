"""Readiness against a real IBKR Paper account (brief sections 6D, 6E, 32).

**Read-only.** Nothing in this file submits an order; the gated order test is
``readiness paper``, which is a command rather than a test and is behind four
separate switches.

Skipped unless ``ALLOW_LIVE_TESTS=true``. Skipping is honest: the readiness gate
reports the broker criteria ``NOT_TESTED``, which is not a pass, and section 32
is explicit that READY_FOR_PAPER cannot be claimed without a reachable gateway.

One known environment constraint, worth stating because it looks like a bug:
the shipped ``ib-gateway`` image trusts only ``127.0.0.1`` for API access
(``jts.ini``'s ``TrustedIPs``). A connection to the gateway's own API port
(paper 4002) from anywhere but loopback is accepted at the TCP level and then
dropped without an API answer — indistinguishable from a hang. The image runs
``socat TCP-LISTEN:4004,fork TCP:127.0.0.1:4002`` for exactly this reason, and
``docker-compose.yml`` publishes that socat port (``${IBKR_PORT:-4002}:4004``),
so a developer running these tests on the host connects to ``127.0.0.1:4002``
as usual and socat re-originates from loopback. The runtime container solves
the same problem the other way, with ``network_mode: "service:ib-gateway"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_system.domain.enums import BrokerReadStatus, ReadinessStatus
from trading_system.infrastructure.settings import Settings, load_config
from trading_system.readiness.collectors import collect_broker, collect_reconciliation

pytestmark = [pytest.mark.integration, pytest.mark.ibkr]

REPO = Path(__file__).resolve().parents[2]
NOW = __import__("datetime").datetime.now(__import__("datetime").UTC)


@pytest.fixture(scope="module")
def paper_settings() -> Settings:
    settings = Settings()
    if settings.trading_mode.value != "PAPER":
        pytest.fail(
            f"TRADING_MODE is {settings.trading_mode.value}; these tests read a paper "
            f"account and run against PAPER only."
        )
    return settings


@pytest.fixture(scope="module")
def config():
    return load_config(REPO / "config")


@pytest.fixture(scope="module")
def broker_record(paper_settings: Settings, config, tmp_path_factory):
    """One short-lived READ-ONLY connection, four handshake-cached reads."""
    root = tmp_path_factory.mktemp("readiness-paper")
    record = collect_broker(
        settings=paper_settings, config=config, project_root=root, observed_at=NOW
    )
    if not record.collected:
        pytest.skip(f"no IBKR Paper gateway is reachable: {record.error}")
    return record


# ---------------------------------------------------------------------------
# Connectivity and the account
# ---------------------------------------------------------------------------
def test_the_paper_gateway_is_reachable(broker_record) -> None:
    assert broker_record.detail["connected"] is True
    assert broker_record.detail["trading_mode"] == "PAPER"


def test_the_connection_is_read_only(broker_record) -> None:
    """``build_broker`` is read-only whatever the settings say."""
    assert broker_record.detail["read_only"] is True


def test_reading_the_account_submits_no_orders(broker_record) -> None:
    """Asserted off the broker's own counter, not assumed."""
    assert broker_record.detail["orders_submitted"] == 0


@pytest.mark.parametrize(
    "key",
    ["account_status", "positions_status", "orders_status", "executions_status"],
)
def test_every_read_reports_a_definite_status(broker_record, key: str) -> None:
    """Brief section 6E. ``EMPTY`` is an answer; ``UNAVAILABLE`` is not."""
    status = broker_record.detail[key]
    assert status in {BrokerReadStatus.OK.value, BrokerReadStatus.EMPTY.value}, (
        f"{key} came back {status}: 'we could not look' is not 'there is nothing there'"
    )


def test_the_account_number_is_masked(broker_record) -> None:
    """Brief section 6E: no account number in a stored artifact."""
    account = broker_record.detail["account"]
    if account is None:
        pytest.skip("the broker reported no account id to mask")
    assert "*" in account
    assert len(account.replace("*", "")) <= 4


def test_the_broker_criteria_pass_against_a_live_gateway(broker_record, policy=None) -> None:
    """The criteria, judged against the record the gateway actually produced."""
    from trading_system.domain.enums import ReadinessCriterionId
    from trading_system.readiness.criteria import criterion

    for which in (
        ReadinessCriterionId.PAPER_BROKER_REACHABLE,
        ReadinessCriterionId.ACCOUNT_READABLE,
        ReadinessCriterionId.POSITIONS_READABLE,
        ReadinessCriterionId.ORDERS_READABLE,
        ReadinessCriterionId.FILLS_READABLE,
    ):
        verdict = criterion(which).predicate(broker_record)
        assert verdict.status is ReadinessStatus.PASS, f"{which.value}: {verdict.detail}"


# ---------------------------------------------------------------------------
# Reconciliation (brief section 6F)
# ---------------------------------------------------------------------------
def test_reconciliation_runs_and_submits_nothing(
    paper_settings: Settings, config, tmp_path: Path
) -> None:
    """It compares and REPORTS. It never adopts, cancels or corrects."""
    record = collect_reconciliation(
        settings=paper_settings, config=config, project_root=tmp_path, observed_at=NOW
    )
    if not record.collected:
        pytest.skip(f"reconciliation could not run: {record.error}")
    assert record.detail["orders_submitted"] == 0
    assert record.detail["corrective_orders"] == 0


def test_a_pre_existing_position_is_reported_and_left_alone(
    paper_settings: Settings, config, tmp_path: Path
) -> None:
    """Brief section 6F: record them, report them, do not modify them.

    A paper account with holdings nothing in our ledger accounts for produces
    ``ORPHAN_BROKER_POSITION`` findings. That is the correct outcome and this
    asserts only that nothing was adopted or traded to make it go away.
    """
    record = collect_reconciliation(
        settings=paper_settings, config=config, project_root=tmp_path, observed_at=NOW
    )
    if not record.collected:
        pytest.skip(f"reconciliation could not run: {record.error}")
    orphans = record.detail.get("orphan_positions", 0)
    if orphans:
        assert record.detail["corrective_orders"] == 0
        assert record.detail["orders_submitted"] == 0


def test_an_unobservable_broker_is_never_a_match(
    paper_settings: Settings, config, tmp_path: Path
) -> None:
    """Whatever the outcome, ``BROKER_DATA_UNAVAILABLE`` cannot read as agreement."""
    from trading_system.domain.enums import ReadinessCriterionId
    from trading_system.readiness.criteria import criterion

    record = collect_reconciliation(
        settings=paper_settings, config=config, project_root=tmp_path, observed_at=NOW
    )
    if not record.collected:
        pytest.skip(f"reconciliation could not run: {record.error}")
    verdict = criterion(ReadinessCriterionId.RECONCILIATION_RUNS).predicate(record)
    if record.detail.get("status") == "BROKER_DATA_UNAVAILABLE":
        # UNKNOWN, and therefore not satisfied. Both stated: the status is the
        # honest one, and `satisfied` is the property every gate reads.
        assert verdict.status is ReadinessStatus.UNKNOWN
        assert verdict.status not in {ReadinessStatus.PASS}
