"""The gated paper-order authorisation check (Milestone 12, sections 9, 33, 34).

**This module opens no connection and submits no order**, and that is a
deliberate reversal of where this milestone started.

The first shape of it built a writable broker and sent a controlled order
directly. That worked, and it broke a Milestone 8 invariant that
``tests/execution/test_boundaries.py`` and ``tests/positions/test_boundaries.py``
both assert:

    ``build_execution_broker`` is the only writable broker constructor, and
    ``execution/service.py`` is its only caller.

Two order paths is exactly one more than a system with this one's safety posture
should have, and brief section 2 is explicit that Milestone 12 must not weaken
an existing gate. The audited path already exists and is already validated end
to end by ``tests/integration/test_paper_execution.py`` — one controlled order,
far out of the money, submitted, observed, cancelled, on three short-lived
connections, behind two environment unlocks.

So what lives here is the part that was actually missing: the **authorisation
check**. Four independent gates, none implying another, with a refusal that
names what is missing:

.. code-block:: text

    1. readiness.paper_execution.enabled          config/readiness.yaml, ships false
    2. ALLOW_LIVE_TESTS=true                      environment
    3. RUN_PAPER_EXECUTION_TESTS=true             environment
    4. an explicit authorisation flag             NOT --confirm

The fourth is separate from ``--confirm`` on purpose (section 33). ``--confirm``
already means "send the order this execution run built"; reusing it would mean a
developer who typed it for an entry had thereby authorised a second thing, and
two different actions must not share one word.

``TRADING_MODE`` must be ``PAPER``. LIVE is refused here, in
``config/readiness.yaml``, in ``config/execution.yaml``, in the broker factory
and in the adapter — five refusals for the one irreversible action this system
can take.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from trading_system.domain.enums import TradingMode
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import Settings, SystemConfig

__all__ = [
    "WARNING_TEXT",
    "PaperGateAuthorization",
    "PaperGateRefusedError",
    "authorize_paper_validation",
    "check_gates",
]

_logger = get_logger(__name__)

#: Printed before anything else (brief section 33). No silent submission.
WARNING_TEXT = """\
WARNING
The paper validation submits a REAL order to the IBKR PAPER account.
No LIVE order is permitted: TRADING_MODE must be PAPER, and LIVE is refused in
configuration, in the broker factory and in the adapter.

Exactly ONE order is submitted. It is far out of the money and priced far below
anything that could trade, and it is cancelled before the run finishes. If it
fills anyway the fill is RECORDED and reported - no opposite order is sent, and
cleanup is left to a human.

This command checks the authorisations. It does not itself send anything."""

#: The one audited path that may place an order against a paper account.
#:
#: Named here so the refusal and the go-ahead both point at the same command,
#: and so nobody has to guess which of several ways is the sanctioned one.
SANCTIONED_COMMAND = (
    "ALLOW_LIVE_TESTS=true RUN_PAPER_EXECUTION_TESTS=true IBKR_READ_ONLY=false \\\n"
    "  .venv/bin/pytest -m paper_execution -s      # or: make test-paper-execution"
)


class PaperGateRefusedError(RuntimeError):
    """A gate refused. Nothing was authorised and nothing was sent."""


@dataclass(frozen=True, slots=True)
class PaperGateAuthorization:
    """The result of checking every gate.

    Reports in the terms brief section 10 asks for, minus the fields only an
    actual submission can fill — because this module deliberately does not make
    one, and inventing an ``ORDER_ID`` for an order nobody sent would be
    precisely the fabricated evidence section 39 forbids.
    """

    mode: str
    authorized: bool
    gates: dict[str, bool] = field(default_factory=dict)
    #: Always zero here, and printed rather than assumed.
    orders_submitted: int = 0
    #: How to actually run the validation, once the gates are satisfied.
    next_command: str = SANCTIONED_COMMAND
    detail: str = ""


def check_gates(*, settings: Settings, config: SystemConfig, authorized: bool) -> None:
    """Verify every gate. Raises :class:`PaperGateRefusedError` on the first failure.

    The order is most-fundamental first: the mode before the flags, because
    being pointed at LIVE is a different kind of problem from having forgotten
    an environment variable, and the operator should be told the worse one.
    """
    if settings.trading_mode is not TradingMode.PAPER:
        raise PaperGateRefusedError(
            f"TRADING_MODE is {settings.trading_mode.value}. The paper validation runs "
            f"against PAPER only. Nothing was authorised."
        )

    policy = config.readiness.paper_execution
    if not policy.enabled:
        raise PaperGateRefusedError(
            "readiness.paper_execution.enabled is false in config/readiness.yaml. This is "
            "the shipped default: submitting a real order is a deliberate act, not one "
            "inherited from a configuration nobody edited."
        )

    if os.environ.get("ALLOW_LIVE_TESTS", "false").lower() != "true":
        raise PaperGateRefusedError("ALLOW_LIVE_TESTS is not set. Nothing was authorised.")

    if os.environ.get("RUN_PAPER_EXECUTION_TESTS", "false").lower() != "true":
        raise PaperGateRefusedError(
            "RUN_PAPER_EXECUTION_TESTS is not set. Two environment variables are required "
            "and neither implies the other: unlocking the gateway for a read-only "
            "diagnostic must not also authorise an order. Nothing was authorised."
        )

    if not authorized:
        raise PaperGateRefusedError(
            "the readiness paper authorisation flag was not given. This is deliberately "
            "NOT --confirm: that flag already authorises an ordinary execution run, and "
            "one word must not authorise two different actions. Nothing was authorised."
        )

    if settings.ibkr_read_only:
        raise PaperGateRefusedError(
            "IBKR_READ_ONLY is true, so the broker adapter refuses to place orders. Set it "
            "to false explicitly for the validation run. Nothing was authorised."
        )

    if config.execution.allow_live or not config.execution.paper_only:
        raise PaperGateRefusedError(
            "config/execution.yaml permits live execution. The readiness gate refuses to "
            "authorise anything against a configuration that has removed one of the live "
            "refusals. Nothing was authorised."
        )


def authorize_paper_validation(
    *, settings: Settings, config: SystemConfig, authorized: bool
) -> PaperGateAuthorization:
    """Check every gate and report what is authorised. Sends nothing.

    On success this hands back :data:`SANCTIONED_COMMAND` — the one audited
    order path — rather than opening a connection of its own. That path is
    ``tests/integration/test_paper_execution.py``, which already validates a
    real paper submission end to end and does so through
    ``execution/service.py``, the only caller of ``build_execution_broker``.
    """
    check_gates(settings=settings, config=config, authorized=authorized)

    _logger.info(
        "readiness.paper_gate.authorized",
        mode=settings.trading_mode.value,
        detail=(
            "every paper-validation gate passed. Nothing was sent by this command; the "
            "audited submission path is the paper_execution suite"
        ),
    )
    return PaperGateAuthorization(
        mode=settings.trading_mode.value,
        authorized=True,
        gates={
            "trading_mode_is_paper": True,
            "readiness_paper_execution_enabled": True,
            "allow_live_tests": True,
            "run_paper_execution_tests": True,
            "explicit_authorization_flag": True,
            "ibkr_read_only_disabled": True,
            "execution_config_refuses_live": True,
        },
        detail=(
            "Every gate is satisfied. This command sends nothing: the audited order path "
            "runs through execution/service.py, the only caller of build_execution_broker, "
            "and a second path would weaken a Milestone 8 invariant that two boundary "
            "suites assert."
        ),
    )
