"""What telemetry may never carry, proved against real spans.

Telemetry is not an audit archive. It leaves this process, is retained by
systems with different access controls, and is frequently the least-locked-down
thing in a deployment. These tests assert the redaction against the spans the
system actually emits rather than by inspection — the first line of defence is
that the attribute vocabulary defines no name for any of this, and this is the
second, because the first depends on everybody using the vocabulary.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.observability.privacy import (
    DEFAULT_FORBIDDEN_SUBSTRINGS,
    PrivacyPolicy,
    mask_account,
    sanitize,
    would_emit,
)
from trading_system.observability.provider import RecordingTelemetry
from trading_system.observability.tracing import (
    operation,
    reset_provider,
    set_privacy_policy,
    set_provider,
)

pytestmark = pytest.mark.unit

ACCOUNT = "DU1234567"


@pytest.fixture
def telemetry():
    """A recording provider, torn down so no test leaks one into another."""
    recorder = RecordingTelemetry()
    set_provider(recorder)
    set_privacy_policy(PrivacyPolicy())
    yield recorder
    reset_provider()


# ---------------------------------------------------------------------------
# The forbidden names
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "password",
        "ibkr_password",
        "api_key",
        "anthropic_api_key",
        "secret",
        "client_secret",
        "token",
        "telegram_bot_token",
        "credential",
        "prompt",
        "system_prompt",
        "completion",
        "llm_completion",
        "response_text",
        "portfolio",
        "full_portfolio",
        "balance",
        "account_balance",
        "account_number",
    ],
)
def test_a_forbidden_attribute_never_reaches_a_span(telemetry, name: str) -> None:
    with operation("exit.evaluate", attributes={name: "sensitive", "trading.status": "WAIT"}):
        pass

    span = telemetry.span_named("exit.evaluate")
    assert name not in span.attributes
    assert span.attributes["trading.status"] == "WAIT"


def test_the_whole_forbidden_vocabulary_is_refused() -> None:
    for substring in DEFAULT_FORBIDDEN_SUBSTRINGS:
        assert not would_emit(substring)
        assert not would_emit(f"trading.{substring}.value")


def test_a_secret_is_dropped_rather_than_masked(telemetry) -> None:
    """A masked credential is still a credential-shaped thing in a backend."""
    with operation("research.run", attributes={"anthropic_api_key": "sk-ant-123"}):
        pass

    span = telemetry.span_named("research.run")
    assert not any("sk-ant" in str(value) for value in span.attributes.values())
    assert "anthropic_api_key" not in span.attributes


# ---------------------------------------------------------------------------
# Account identifiers
# ---------------------------------------------------------------------------
def test_an_account_number_is_masked_wherever_it_appears(telemetry) -> None:
    """Masked on the *shape* of the value, so it cannot leak under a name
    nobody thought to forbid."""
    with operation("broker.observe", attributes={"trading.detail": f"account {ACCOUNT} read"}):
        pass

    span = telemetry.span_named("broker.observe")
    assert ACCOUNT not in str(span.attributes)
    assert "*****4567" in span.attributes["trading.detail"]


def test_masking_keeps_enough_to_tell_two_accounts_apart() -> None:
    assert mask_account("DU1234567") == "*****4567"
    assert mask_account("DU1234567") != mask_account("DU7654321")


def test_masking_cannot_be_switched_off() -> None:
    from trading_system.infrastructure.settings import ObservabilityPrivacyConfig

    with pytest.raises(ValueError, match="mask_account_identifiers"):
        ObservabilityPrivacyConfig(mask_account_identifiers=False)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["capital_committed", "entry_cost", "realized_pnl", "market_value", "premium", "cash"],
)
def test_a_monetary_attribute_is_dropped_by_default(telemetry, name: str) -> None:
    """Counts, durations, statuses and references answer operational questions.
    A money figure in a trace is financial truth in the wrong system."""
    with operation("pnl.compute", attributes={name: Decimal("1210.00")}):
        pass

    assert name not in telemetry.span_named("pnl.compute").attributes


def test_configuration_can_permit_monetary_attributes_explicitly() -> None:
    permissive = PrivacyPolicy(allow_monetary_attributes=True)

    assert sanitize({"entry_cost": Decimal("1210.00")}, permissive) == {"entry_cost": "1210.00"}
    assert sanitize({"entry_cost": Decimal("1210.00")}, PrivacyPolicy()) == {}


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------
def test_a_long_value_is_truncated_visibly(telemetry) -> None:
    """A long value in a span is a payload trying to become an archive."""
    with operation("research.run", attributes={"trading.detail": "x" * 5000}):
        pass

    value = telemetry.span_named("research.run").attributes["trading.detail"]
    assert len(value) <= 256
    assert value.endswith("…")


# ---------------------------------------------------------------------------
# The filter must never raise
# ---------------------------------------------------------------------------
def test_a_value_whose_str_raises_is_dropped_rather_than_propagated() -> None:
    """A redaction bug must not take down the operation it was observing."""

    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert sanitize({"trading.status": Hostile(), "trading.mode": "PAPER"}) == {
        "trading.mode": "PAPER"
    }


def test_none_values_are_dropped_rather_than_emitted_as_null() -> None:
    assert sanitize({"trading.exit.id": None, "trading.status": "WAIT"}) == {
        "trading.status": "WAIT"
    }


def test_an_absent_configuration_leaves_the_strictest_policy_in_force() -> None:
    """The failure mode of the alternative is a deployment that emits
    everything because a YAML key was misspelled."""
    policy = PrivacyPolicy.of(None)

    assert policy.forbidden_substrings == DEFAULT_FORBIDDEN_SUBSTRINGS
    assert policy.mask_account_identifiers is True
    assert policy.allow_monetary_attributes is False


# ---------------------------------------------------------------------------
# What AI telemetry carries
# ---------------------------------------------------------------------------
def test_an_llm_span_carries_cost_and_outcome_but_no_content() -> None:
    """The answer belongs in the immutable artifact, where it can be audited
    and where nobody has to trust a telemetry retention policy with it."""
    from trading_system.observability.llm import llm_span

    recorder = RecordingTelemetry()
    set_provider(recorder)
    set_privacy_policy(PrivacyPolicy())
    try:

        class _Identity:
            provider = "anthropic"
            model_name = "claude-test"
            agent_version = "1.0.0+abc"

        class _Client:
            identity = _Identity()

        class _Response:
            input_tokens = 1200
            output_tokens = 300
            latency_ms = 850.0
            stop_reason = "end_turn"
            text = "SECRET MODEL REASONING THAT MUST NOT LEAVE"

        with llm_span(agent="market_researcher", client=_Client()) as call:
            call.record(_Response())
    finally:
        reset_provider()

    span = recorder.span_named("llm.generate")
    assert span is not None
    assert span.attributes["agent.name"] == "market_researcher"
    assert span.attributes["llm.model"] == "claude-test"
    assert span.attributes["llm.input_tokens"] == 1200
    assert span.attributes["llm.output_tokens"] == 300
    assert not any("SECRET MODEL REASONING" in str(value) for value in span.attributes.values())


def test_the_attribute_vocabulary_defines_no_name_for_a_secret() -> None:
    """The first line of defence: there is no name to put them under."""
    from trading_system.observability import attributes

    names = [
        value
        for name, value in vars(attributes).items()
        if not name.startswith("_") and isinstance(value, str)
    ]
    for name in names:
        assert would_emit(name), name
