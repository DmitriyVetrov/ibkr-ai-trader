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


class _DeferredLogger:
    """A logger that resolves its processor chain on every call.

    Exists because ``structlog``'s lazy proxy is only lazy until something
    binds to it. ``BoundLoggerLazyProxy.bind()`` *assembles* a concrete bound
    logger and snapshots ``_CONFIG.default_processors`` into it — so a
    module-level ``_logger = get_logger(__name__)`` freezes the processor chain
    as it stood at **import time**.

    That was silently wrong once telemetry arrived. Milestone 11's trace
    correlation is installed by a processor that
    :func:`~trading_system.observability.logging.install_correlation` adds to
    the chain *after* start-up, so every logger bound before that call kept
    emitting lines with no ``trace_id`` and no ``span_id``. The lines still
    appeared and still looked right, which is exactly why nobody noticed: the
    failure mode of missing correlation is a log that reads perfectly and
    cannot be joined to its trace. Milestone 12's acceptance gate found it by
    asking the log backend for a specific trace id and getting nothing.

    Re-resolving per call is what ``cache_logger_on_first_use=False`` does
    anyway, and ``install_correlation`` already sets that. The cost is one
    dictionary assembly per log statement; the alternative is telemetry that
    is quietly half-connected.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def _bound(self) -> Any:
        return structlog.get_logger(self._name).bind(logger=self._name)

    def __getattr__(self, attribute: str) -> Any:
        return getattr(self._bound(), attribute)

    def bind(self, **values: Any) -> Any:
        return self._bound().bind(**values)

    def __repr__(self) -> str:
        return f"_DeferredLogger({self._name!r})"


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger, configuring logging on first use.

    The name is bound into the event context rather than left for a processor
    to read off the logger object: ``PrintLogger`` has no name, and binding is
    the only way to keep the module name in the record.

    Named loggers come back as a :class:`_DeferredLogger` so that a processor
    installed after import — trace correlation, in particular — still reaches
    them. See that class for what went wrong when they did not.
    """
    if not _configured:
        configure_logging()
    return _DeferredLogger(name) if name else structlog.get_logger()
