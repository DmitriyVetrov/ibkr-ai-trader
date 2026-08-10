"""Structured logging setup.

Logs are structured because they are an operational record of an autonomous
system that trades unattended: "why did it not trade at 15:30" has to be
answerable from the log alone.

Timestamps are ISO-8601 UTC, matching every other timestamp in the system.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

__all__ = ["configure_logging", "get_logger"]

_configured = False


def configure_logging(level: str = "INFO", log_format: str = "console") -> None:
    """Configure structlog and the stdlib root logger.

    Args:
        level: standard logging level name.
        log_format: ``console`` for human-readable local development,
            ``json`` for machine-parseable output in the container runtime.
    """
    global _configured

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if log_format.lower() == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=numeric_level, force=True)
    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger, configuring logging on first use."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
