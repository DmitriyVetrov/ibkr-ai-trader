"""Structured logging actually logs.

An autonomous system's log is its operational record: "why did it not trade at
15:30" has to be answerable from the log alone. That only works if a log call
succeeds, which is less obvious than it sounds — ``structlog``'s stdlib
``add_logger_name`` processor reads ``logger.name``, and the ``PrintLogger``
this configuration uses does not have one, so every log call raised
``AttributeError`` until the processor was replaced.
"""

from __future__ import annotations

import pytest

from trading_system.infrastructure.logging import configure_logging, get_logger

pytestmark = pytest.mark.unit


def test_logging_a_structured_event_does_not_raise(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="json")
    logger = get_logger("trading_system.tests")

    logger.info("data.collection", provider="SIMULATOR", symbol="SPY", records_received=1)

    captured = capsys.readouterr().err
    assert "data.collection" in captured
    assert "SIMULATOR" in captured


def test_the_logger_name_is_recorded(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="json")
    get_logger("trading_system.data.collectors.base").info("collected")

    assert "trading_system.data.collectors.base" in capsys.readouterr().err


def test_an_unnamed_logger_still_works(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="json")
    get_logger().info("anonymous")

    assert "anonymous" in capsys.readouterr().err


def test_console_format_also_works(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", log_format="console")
    get_logger("trading_system.tests").info("console.event", symbol="SPY")

    assert "console.event" in capsys.readouterr().err
