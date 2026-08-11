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


def _add_logger_name(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Add the logger's name when it has one.

    ``structlog.stdlib.add_logger_name`` assumes a stdlib logger and reads
    ``logger.name`` unconditionally. This configuration uses
    ``PrintLoggerFactory``, whose loggers have no ``name`` — so the stdlib
    processor raises ``AttributeError`` on the first log call, turning any
    logging statement into a crash. The name is bound by :func:`get_logger`
    instead, and this processor only fills it in when the logger itself can
    supply one.
    """
    if "logger" not in event_dict:
        name = getattr(logger, "name", None)
        if name:
            event_dict["logger"] = name
    return event_dict


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
        _add_logger_name,
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
    """Return a bound structlog logger, configuring logging on first use.

    The name is bound into the event context rather than left for a processor
    to read off the logger object: ``PrintLogger`` has no name, and binding is
    the only way to keep the module name in the record.
    """
    if not _configured:
        configure_logging()
    logger = structlog.get_logger(name)
    return logger.bind(logger=name) if name else logger
