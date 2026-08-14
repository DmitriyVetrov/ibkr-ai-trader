"""Outbound notification channels. Never authoritative state (Milestone 11).

A :class:`NotificationProvider` is an adapter and nothing else. It receives an
:class:`~trading_system.operations.models.Alert` that has already been decided
and already been stored, and tries to put it somewhere a person will see it.
Three properties follow, and all three matter:

* **Delivery is best effort and never propagates.** A provider that raises is
  caught, recorded as a delivery failure, and the caller continues. An alert
  about a broker outage must not be able to *cause* a second outage by taking
  down whatever was reporting it.
* **A failure to send is not a failure to alert.** The alert is persisted
  before any channel sees it. "Nobody was told" and "it did not happen" are
  different facts, and only the first is a notification problem.
* **No vendor appears above the adapter line.** ``console``, ``file`` and
  ``webhook`` are the shipped kinds. Telegram, Slack and e-mail are *webhook
  adapters* — a URL in an environment variable, never in configuration, because
  a webhook URL usually embeds a token and ``config/alerts.yaml`` is committed.

The webhook provider uses ``urllib`` from the standard library rather than a
dependency, and is the only place in this package that opens a socket. That is
why it lives here and not in :mod:`trading_system.operations.alerts`: the rule
evaluation must stay reachable from a boundary test that forbids sockets, and
the thing that sends must be separable from the thing that decides.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trading_system.domain.enums import AlertSeverity
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import NotificationChannelConfig
from trading_system.operations.models import Alert

__all__ = [
    "ConsoleNotificationProvider",
    "DeliveryResult",
    "FileNotificationProvider",
    "NotificationProvider",
    "NullNotificationProvider",
    "WebhookNotificationProvider",
    "build_providers",
]

_logger = get_logger(__name__)

#: Severity ordering, so a channel's minimum can be compared.
_ORDER = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What happened when one channel was offered one alert."""

    channel: str
    delivered: bool
    skipped: bool = False
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.delivered and not self.skipped


class NotificationProvider(ABC):
    """Somewhere an alert can be sent.

    The interface is one method. Anything richer — acknowledgement, threading,
    escalation — would make a notification channel into a piece of operational
    state, and operational state belongs in the store.
    """

    #: Stable channel name, recorded on the alert once delivery succeeds.
    name: str = "provider"
    minimum_severity: AlertSeverity = AlertSeverity.WARNING

    @abstractmethod
    def send(self, alert: Alert) -> None:
        """Deliver one alert, or raise. Raising is caught by :meth:`notify`."""

    def accepts(self, alert: Alert) -> bool:
        return _ORDER[alert.severity] >= _ORDER[self.minimum_severity]

    def notify(self, alert: Alert) -> DeliveryResult:
        """Offer one alert to this channel. Never raises.

        The whole reason this wrapper exists: an alerting path that could raise
        would let a notification failure interrupt whatever was reporting the
        condition — most likely a health check running inside a scheduler tick,
        which would then fail, which would raise another alert.
        """
        if not self.accepts(alert):
            return DeliveryResult(channel=self.name, delivered=False, skipped=True)
        try:
            self.send(alert)
        except Exception as exc:
            _logger.warning(
                "notifications.delivery_failed",
                channel=self.name,
                alert_id=alert.alert_id,
                code=alert.code.value,
                error=str(exc),
            )
            return DeliveryResult(channel=self.name, delivered=False, error=str(exc))
        return DeliveryResult(channel=self.name, delivered=True)


class NullNotificationProvider(NotificationProvider):
    """Accepts everything and does nothing. The default when nothing is configured."""

    name = "null"
    minimum_severity = AlertSeverity.INFO

    def send(self, alert: Alert) -> None:
        return None


class ConsoleNotificationProvider(NotificationProvider):
    """Writes one line per alert to the structured log.

    Deliberately the structured log rather than ``print``: an operational
    notification should carry the same trace correlation as everything else, so
    an operator who sees the alert can find the trace that produced it.
    """

    name = "console"

    def __init__(self, *, minimum_severity: AlertSeverity = AlertSeverity.WARNING) -> None:
        self.minimum_severity = minimum_severity

    def send(self, alert: Alert) -> None:
        _logger.warning(
            "alert",
            alert_id=alert.alert_id,
            code=alert.code.value,
            category=alert.category.value,
            severity=alert.severity.value,
            subject=alert.subject,
            summary=alert.summary,
            occurrences=alert.occurrences,
            threshold=alert.threshold,
            trading_mode=alert.trading_mode.value,
            action=alert.recommended_action,
        )


class FileNotificationProvider(NotificationProvider):
    """Appends one JSON line per alert to a file under the data root.

    An append-only record of what was *notified*, distinct from the alert store
    itself, which records what was *raised*. The difference between the two
    files is exactly the set of alerts nobody was told about.
    """

    name = "file"

    def __init__(self, path: Path, *, minimum_severity: AlertSeverity = AlertSeverity.INFO) -> None:
        self._path = Path(path)
        self.minimum_severity = minimum_severity

    @property
    def path(self) -> Path:
        return self._path

    def send(self, alert: Alert) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "alert_id": alert.alert_id,
            "code": alert.code.value,
            "category": alert.category.value,
            "severity": alert.severity.value,
            "subject": alert.subject,
            "summary": alert.summary,
            "raised_at": alert.raised_at.isoformat(),
            "occurrences": alert.occurrences,
            "threshold": alert.threshold,
            "trading_mode": alert.trading_mode.value,
            "references": alert.references,
            "recommended_action": alert.recommended_action,
        }
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")


class WebhookNotificationProvider(NotificationProvider):
    """Posts one JSON body per alert to a URL held in an environment variable.

    The adapter every vendor integration goes through. The URL comes from the
    environment because a webhook URL usually *is* the credential — Telegram's
    bot endpoint and Slack's incoming-webhook both embed a token in the path —
    and ``config/alerts.yaml`` is committed.

    Bounded, and silent about its own failures beyond a log line: an
    unreachable webhook is an observability problem, and this whole path is
    wrapped by :meth:`NotificationProvider.notify` so it cannot become anything
    more than that.
    """

    name = "webhook"

    def __init__(
        self,
        env_var: str,
        *,
        timeout_seconds: float = 5.0,
        minimum_severity: AlertSeverity = AlertSeverity.CRITICAL,
    ) -> None:
        self._env_var = env_var
        self._timeout = timeout_seconds
        self.minimum_severity = minimum_severity

    @property
    def configured(self) -> bool:
        return bool(os.environ.get(self._env_var, "").strip())

    def send(self, alert: Alert) -> None:
        url = os.environ.get(self._env_var, "").strip()
        if not url:
            raise RuntimeError(
                f"{self._env_var} is not set, so no webhook destination is configured. The URL "
                f"is read from the environment rather than from config/alerts.yaml because it "
                f"usually embeds a token"
            )
        # Imported here, not at module scope. This is the only socket in the
        # operations package, and keeping the import inside the one method that
        # needs it keeps `urllib` out of the import graph of anything that
        # merely evaluates a rule.
        import urllib.request

        body = json.dumps(
            {
                "alert_id": alert.alert_id,
                "code": alert.code.value,
                "severity": alert.severity.value,
                "subject": alert.subject,
                "summary": alert.summary,
                "occurrences": alert.occurrences,
                "trading_mode": alert.trading_mode.value,
                "raised_at": alert.raised_at.isoformat(),
                "recommended_action": alert.recommended_action,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            if response.status >= 400:
                raise RuntimeError(f"webhook responded {response.status}")


def build_providers(
    channels: list[NotificationChannelConfig], *, data_root: Path
) -> list[NotificationProvider]:
    """Construct the enabled channels from configuration.

    A channel whose configuration is unusable is *skipped with a log line*
    rather than raising: a malformed notification destination must not stop the
    system that was trying to report through it.
    """
    providers: list[NotificationProvider] = []
    for channel in channels:
        if not channel.enabled:
            continue
        try:
            providers.append(_build(channel, data_root=data_root))
        except Exception as exc:  # pragma: no cover - defensive
            _logger.warning(
                "notifications.channel_unavailable",
                kind=channel.kind,
                error=str(exc),
                detail="the channel was skipped; alerts are still recorded",
            )
    return providers


def _build(channel: NotificationChannelConfig, *, data_root: Path) -> NotificationProvider:
    if channel.kind == "console":
        return ConsoleNotificationProvider(minimum_severity=channel.minimum_severity)
    if channel.kind == "file":
        target = channel.target or "alerts/notified.jsonl"
        path = Path(target)
        if not path.is_absolute():
            path = data_root / path
        return FileNotificationProvider(path, minimum_severity=channel.minimum_severity)
    if channel.kind == "webhook":
        return WebhookNotificationProvider(
            channel.target or "ALERT_WEBHOOK_URL",
            timeout_seconds=channel.timeout_seconds,
            minimum_severity=channel.minimum_severity,
        )
    raise ValueError(f"unknown notification channel kind {channel.kind!r}")


def notify_all(
    providers: list[NotificationProvider], alert: Alert
) -> tuple[list[str], list[DeliveryResult]]:
    """Offer one alert to every channel. Returns what accepted it, and every result.

    Never raises, whatever any provider does. The channel names that succeeded
    are recorded on the stored alert so an operator can tell "raised and sent"
    from "raised and nobody was told" — which is the distinction that makes a
    quiet alerting channel discoverable.
    """
    results = [provider.notify(alert) for provider in providers]
    return [result.channel for result in results if result.delivered], results


def as_dict(result: DeliveryResult) -> dict[str, Any]:
    """One delivery result, flattened for logging."""
    return {
        "channel": result.channel,
        "delivered": result.delivered,
        "skipped": result.skipped,
        "error": result.error,
    }
