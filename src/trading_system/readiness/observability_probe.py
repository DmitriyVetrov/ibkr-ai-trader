"""Runtime acceptance for the observability stack (Milestone 12, brief §11/§31).

Milestone 11 shipped the collector, Tempo, Prometheus, Loki and Grafana with
every one of them recorded ``NOT_TESTED``. Closing that is a named M12
acceptance gate, and closing it *honestly* means one thing above all others:

    **A validated compose file is not proof that a stack is running.**

So every probe here is an HTTP request to a running service. Nothing in this
module inspects a YAML file and concludes a backend is healthy, and nothing
concludes a signal arrived because the application sent one — arrival is
established by asking the backend.

.. code-block:: text

    emit a real span, metric and log
          |  OTLP
    otel-collector      -> its own /metrics says how many spans it ACCEPTED
          |
    Tempo               -> GET /api/traces/<trace_id>
    Prometheus          -> GET /api/v1/query?query=<metric>
    Loki                -> GET /loki/api/v1/query_range
    Grafana             -> GET /api/health, /api/datasources, /api/search

This module imports ``urllib`` and is therefore **deliberately quarantined**
from everything the evaluator can reach. The research agent, the risk engine,
the exit engine and the strategy selector all have boundary tests forbidding
``socket``, ``urllib`` and ``http`` in their transitive graphs, and the
readiness evaluator has the same. Only a collector may import this.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from trading_system.domain.enums import ReadinessEvidenceKind
from trading_system.infrastructure.logging import get_logger
from trading_system.infrastructure.settings import ObservabilityAcceptanceConfig
from trading_system.readiness.evidence import EvidenceRecord

__all__ = [
    "HttpResult",
    "ObservabilityProbe",
    "probe_collector",
    "probe_grafana",
    "probe_loki",
    "probe_prometheus",
    "probe_services",
    "probe_tempo",
]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HttpResult:
    """One HTTP probe. Never raises; a failure is a value."""

    url: str
    status: int | None
    body: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def json(self) -> Any | None:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, ValueError):
            return None


def http_get(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> HttpResult:
    """A bounded GET. Never raises, and never follows a redirect off-host.

    Bounded because an unreachable backend must cost seconds rather than
    hanging a readiness run — the same rule Milestone 2 applies to every broker
    request, for the same reason.
    """
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(1_000_000).decode("utf-8", errors="replace")
            return HttpResult(url=url, status=int(response.status), body=body)
    except urllib.error.HTTPError as exc:
        # An error response still carries a body worth recording — Grafana and
        # Loki both explain a refusal in it, and "401" alone tells a reader
        # nothing about which credential was wrong.
        body = ""
        with suppress(Exception):
            body = exc.read(100_000).decode("utf-8", errors="replace")
        return HttpResult(url=url, status=int(exc.code), body=body, error=str(exc))
    except Exception as exc:
        return HttpResult(url=url, status=None, body="", error=str(exc))


@dataclass(frozen=True, slots=True)
class ObservabilityProbe:
    """Bound endpoints and timeouts for one acceptance run."""

    config: ObservabilityAcceptanceConfig

    @property
    def timeout(self) -> float:
        return self.config.probe_timeout_seconds

    def poll(
        self,
        predicate: Callable[[], tuple[bool, dict[str, Any]]],
        *,
        timeout: float | None = None,
    ) -> tuple[bool, dict[str, Any], float]:
        """Poll until a predicate holds or a deadline passes.

        Signals do not arrive instantly and pretending otherwise would report a
        working pipeline as broken: the collector batches on a five-second
        timeout and Prometheus scrapes every fifteen, so a probe that asked once
        and gave up would fail against a perfectly healthy stack.

        Returns ``(satisfied, last_detail, waited_seconds)`` — the detail from
        the *last* attempt, so a failure reports what was actually seen rather
        than an empty result from an attempt nobody made.
        """
        deadline = timeout if timeout is not None else self.config.propagation_timeout_seconds
        interval = self.config.propagation_poll_seconds
        started = time.monotonic()
        detail: dict[str, Any] = {}
        while True:
            satisfied, detail = predicate()
            waited = time.monotonic() - started
            if satisfied or waited >= deadline:
                return satisfied, detail, waited
            time.sleep(min(interval, max(0.0, deadline - waited)))


# ---------------------------------------------------------------------------
# The stack is up
# ---------------------------------------------------------------------------
def probe_services(probe: ObservabilityProbe, *, observed_at: datetime) -> EvidenceRecord:
    """Every backend answers its own health endpoint.

    Each service is asked the question it can actually answer, rather than all
    of them being pinged on ``/``. Tempo and Loki both serve ``/ready``,
    Prometheus ``/-/healthy``, Grafana ``/api/health``, and the collector
    exposes its own Prometheus-format telemetry — using the right one per
    service is what makes a green result mean the process is *serving* rather
    than merely listening.
    """
    config = probe.config
    checks: dict[str, str] = {
        "otel-collector": config.collector_metrics_url,
        "tempo": f"{config.tempo_url.rstrip('/')}/ready",
        "prometheus": f"{config.prometheus_url.rstrip('/')}/-/healthy",
        "loki": f"{config.loki_url.rstrip('/')}/ready",
        "grafana": f"{config.grafana_url.rstrip('/')}/api/health",
    }
    services: dict[str, bool] = {}
    detail_by_service: dict[str, Any] = {}
    for name, url in checks.items():
        result = http_get(url, timeout=probe.timeout)
        services[name] = result.ok
        detail_by_service[name] = {
            "url": url,
            "status": result.status,
            "error": result.error,
        }
    reachable_any = any(services.values())
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SERVICE_PROBE,
        source="observability stack health endpoints",
        observed_at=observed_at,
        collected=reachable_any,
        error=(
            None
            if reachable_any
            else (
                "no observability service answered. The stack is started with "
                "`docker compose --profile observability up -d`"
            )
        ),
        detail={"services": services, "probes": detail_by_service},
    )


# ---------------------------------------------------------------------------
# The collector received something
# ---------------------------------------------------------------------------
def probe_collector(
    probe: ObservabilityProbe, *, observed_at: datetime, minimum_spans: int = 1
) -> EvidenceRecord:
    """How many spans the collector has *accepted* over OTLP.

    Read from the collector's own self-telemetry
    (``otelcol_receiver_accepted_spans``), which is the only place that
    distinguishes "the collector is up" from "the collector is receiving
    telemetry from this application". Being up is not the same as working, and
    Milestone 11's gap was precisely that nobody had checked the second.
    """

    def attempt() -> tuple[bool, dict[str, Any]]:
        result = http_get(probe.config.collector_metrics_url, timeout=probe.timeout)
        if not result.ok:
            return False, {
                "reachable": False,
                "status": result.status,
                "error": result.error,
                "url": probe.config.collector_metrics_url,
            }
        accepted = _sum_prometheus_metric(result.body, "otelcol_receiver_accepted_spans")
        refused = _sum_prometheus_metric(result.body, "otelcol_receiver_refused_spans")
        exported = _sum_prometheus_metric(result.body, "otelcol_exporter_sent_spans")
        failed = _sum_prometheus_metric(result.body, "otelcol_exporter_send_failed_spans")
        detail = {
            "reachable": True,
            "spans_accepted": accepted,
            "spans_refused": refused,
            "spans_exported": exported,
            "spans_export_failed": failed,
            "url": probe.config.collector_metrics_url,
        }
        return (accepted or 0) >= minimum_spans, detail

    satisfied, detail, waited = probe.poll(attempt)
    detail["waited_seconds"] = round(waited, 1)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SERVICE_PROBE,
        source="otel-collector self-telemetry",
        observed_at=observed_at,
        collected=bool(detail.get("reachable")),
        error=(None if satisfied else str(detail.get("error") or "no spans accepted")),
        detail=detail,
    )


def _sum_prometheus_metric(exposition: str, name: str) -> int | None:
    """Sum every series of one metric in a Prometheus exposition.

    Returns ``None`` when the metric is absent rather than ``0``. "The
    collector reports no such counter" and "the counter is zero" are different
    facts, and only the second is evidence about telemetry flow — the first
    usually means the metric was renamed by an upgrade, which a readiness gate
    should surface rather than silently report as a failure to receive.
    """
    total: int | None = None
    for line in exposition.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not (stripped.startswith(f"{name}{{") or stripped.startswith(f"{name} ")):
            continue
        value = stripped.rsplit(" ", 1)[-1]
        try:
            total = (total or 0) + int(float(value))
        except ValueError:  # pragma: no cover - defensive
            continue
    return total


# ---------------------------------------------------------------------------
# The signal reached each backend
# ---------------------------------------------------------------------------
def probe_tempo(
    probe: ObservabilityProbe, *, trace_id: str | None, observed_at: datetime
) -> EvidenceRecord:
    """A specific, real application trace is retrievable from Tempo by id.

    By id, deliberately, rather than "some trace exists". A backend holding
    somebody else's traces proves nothing about this application's pipeline.
    """
    if not trace_id:
        return EvidenceRecord.of(
            kind=ReadinessEvidenceKind.SERVICE_PROBE,
            source="tempo",
            observed_at=observed_at,
            collected=False,
            error="no trace was emitted, so none could be looked up",
        )
    url = f"{probe.config.tempo_url.rstrip('/')}/api/traces/{urllib.parse.quote(trace_id)}"

    def attempt() -> tuple[bool, dict[str, Any]]:
        result = http_get(url, timeout=probe.timeout)
        payload = result.json() if result.ok else None
        spans = _count_tempo_spans(payload)
        return bool(result.ok and spans), {
            "trace_id": trace_id,
            "trace_found": bool(result.ok and spans),
            "spans": spans,
            "status": result.status,
            "url": url,
            "error": result.error,
        }

    satisfied, detail, waited = probe.poll(attempt)
    detail["waited_seconds"] = round(waited, 1)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SERVICE_PROBE,
        source="tempo /api/traces",
        observed_at=observed_at,
        error=None if satisfied else "the trace was not retrievable from the tracing backend",
        detail=detail,
    )


def _count_tempo_spans(payload: Any) -> int:
    """Count spans in a Tempo trace response, across its response shapes.

    Tempo has answered with both an OTLP-shaped ``batches`` document and a
    Jaeger-shaped ``data`` one across the versions this repository has pinned.
    Handling both is cheaper than pinning a response format nobody controls.
    """
    if not isinstance(payload, dict):
        return 0
    count = 0
    for batch in payload.get("batches", []) or []:
        for scope in batch.get("scopeSpans", []) or batch.get("instrumentationLibrarySpans", []):
            count += len(scope.get("spans", []) or [])
    for trace in payload.get("data", []) or []:
        count += len(trace.get("spans", []) or [])
    return count


def probe_prometheus(
    probe: ObservabilityProbe, *, metric: str, observed_at: datetime
) -> EvidenceRecord:
    """A real application metric is queryable in Prometheus.

    Queried rather than scraped directly off the collector: the point is that
    the *whole* path works — application, OTLP, collector, scrape, storage —
    and reading the collector's exposition would skip the last two hops.
    """
    base = probe.config.prometheus_url.rstrip("/")

    def attempt() -> tuple[bool, dict[str, Any]]:
        url = f"{base}/api/v1/query?query={urllib.parse.quote(metric)}"
        result = http_get(url, timeout=probe.timeout)
        payload = result.json() if result.ok else None
        series: list[Any] = []
        if isinstance(payload, dict) and payload.get("status") == "success":
            series = (payload.get("data") or {}).get("result") or []
        return bool(series), {
            "metric": metric,
            "metric_found": bool(series),
            "series": len(series),
            "sample_labels": sorted((series[0].get("metric") or {}).keys()) if series else [],
            "status": result.status,
            "url": url,
            "error": result.error,
        }

    satisfied, detail, waited = probe.poll(attempt)
    detail["waited_seconds"] = round(waited, 1)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SERVICE_PROBE,
        source="prometheus /api/v1/query",
        observed_at=observed_at,
        error=None if satisfied else f"metric {metric} was not queryable",
        detail=detail,
    )


def probe_loki(
    probe: ObservabilityProbe,
    *,
    query: str,
    observed_at: datetime,
    since_seconds: int = 900,
    expect_substring: str | None = None,
) -> EvidenceRecord:
    """A real structured log line reached Loki.

    ``expect_substring`` is how correlation is established: the caller passes
    the trace id it emitted, and a hit only counts if the stored record
    actually carries it. Matching the stream selector alone would prove that
    *some* log arrived and say nothing about correlation.

    The trace id is looked for in the **stream labels as well as the line
    body**, because that is where it ends up. The application puts ``trace_id``
    in the structlog event dictionary, the OTLP log record carries it as an
    attribute, and the collector's Loki exporter promotes attributes to stream
    labels — so the id is on the record without ever appearing in the rendered
    message. A probe that read only the body reported this working pipeline as
    broken, which is how this comment came to exist.
    """
    base = probe.config.loki_url.rstrip("/")

    def attempt() -> tuple[bool, dict[str, Any]]:
        end = int(time.time() * 1_000_000_000)
        start = end - since_seconds * 1_000_000_000
        url = (
            f"{base}/loki/api/v1/query_range?query={urllib.parse.quote(query)}"
            f"&start={start}&end={end}&limit=200&direction=backward"
        )
        result = http_get(url, timeout=probe.timeout)
        payload = result.json() if result.ok else None
        streams: list[Any] = []
        if isinstance(payload, dict) and payload.get("status") == "success":
            streams = (payload.get("data") or {}).get("result") or []

        lines = 0
        matched = 0
        matched_in_labels = 0
        for stream in streams:
            labels = stream.get("stream") or {}
            label_blob = json.dumps(labels, sort_keys=True)
            label_hit = expect_substring is not None and expect_substring in label_blob
            for entry in stream.get("values") or []:
                if len(entry) < 2:
                    continue
                lines += 1
                body = str(entry[1])
                in_body = expect_substring is not None and expect_substring in body
                if expect_substring is None or in_body or label_hit:
                    matched += 1
                    if label_hit and not in_body:
                        matched_in_labels += 1

        return bool(matched), {
            "query": query,
            "log_found": bool(matched),
            "streams": len(streams),
            "lines": lines,
            "matched_lines": matched,
            "matched_via_labels": matched_in_labels,
            "expect_substring": expect_substring,
            "status": result.status,
            "error": result.error,
        }

    satisfied, detail, waited = probe.poll(attempt)
    detail["waited_seconds"] = round(waited, 1)
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SERVICE_PROBE,
        source="loki /loki/api/v1/query_range",
        observed_at=observed_at,
        error=None if satisfied else "no matching log line was found in the log backend",
        detail=detail,
    )


def probe_grafana(
    probe: ObservabilityProbe,
    *,
    observed_at: datetime,
    required_datasources: tuple[str, ...],
    required_dashboards: tuple[str, ...],
    username: str = "admin",
    password: str = "admin",
) -> EvidenceRecord:
    """Grafana is up, and its datasources and dashboards actually provisioned.

    Asked over the API rather than by reading ``deploy/grafana/provisioning``.
    A provisioning file that Grafana rejected at start-up looks perfect on
    disk, which is exactly the failure mode brief section 11 calls out when it
    says not to accept file inspection as proof.
    """
    import base64

    base = probe.config.grafana_url.rstrip("/")
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {credentials}"}

    health = http_get(f"{base}/api/health", timeout=probe.timeout)
    health_payload = health.json() or {}
    healthy = bool(health.ok and str(health_payload.get("database", "")).lower() == "ok")

    datasources = http_get(f"{base}/api/datasources", timeout=probe.timeout, headers=headers)
    datasource_payload = datasources.json() or []
    found_datasources = sorted(
        str(entry.get("uid") or entry.get("name", "")).lower()
        for entry in datasource_payload
        if isinstance(entry, dict)
    )
    missing_datasources = sorted(
        wanted for wanted in required_datasources if wanted.lower() not in found_datasources
    )

    search = http_get(
        f"{base}/api/search?type=dash-db&limit=100", timeout=probe.timeout, headers=headers
    )
    search_payload = search.json() or []
    found_dashboards = sorted(
        str(entry.get("uid", "")).lower() for entry in search_payload if isinstance(entry, dict)
    )
    missing_dashboards = sorted(
        wanted for wanted in required_dashboards if wanted.lower() not in found_dashboards
    )

    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SERVICE_PROBE,
        source="grafana /api/health, /api/datasources, /api/search",
        observed_at=observed_at,
        collected=health.status is not None,
        error=(None if healthy else (health.error or "Grafana did not report a healthy database")),
        detail={
            "grafana_healthy": healthy,
            "version": health_payload.get("version"),
            "datasources": found_datasources,
            "missing_datasources": missing_datasources,
            "dashboards": found_dashboards,
            "missing_dashboards": missing_dashboards,
            "health_status": health.status,
            "datasources_status": datasources.status,
            "search_status": search.status,
        },
    )


def probe_correlation(
    *,
    trace_id: str | None,
    tempo: EvidenceRecord,
    loki: EvidenceRecord,
    observed_at: datetime,
) -> EvidenceRecord:
    """One trace id, findable in the tracing backend *and* in the log backend.

    Derived from the two probes rather than re-querying, so the correlation
    claim is exactly as strong as the evidence behind it and cannot
    accidentally be stronger.
    """
    return EvidenceRecord.of(
        kind=ReadinessEvidenceKind.SERVICE_PROBE,
        source="tempo + loki, one trace id",
        observed_at=observed_at,
        collected=trace_id is not None,
        error=None if trace_id else "no trace was emitted",
        detail={
            "trace_id": trace_id,
            "trace_found": bool(tempo.detail.get("trace_found")),
            "log_found": bool(loki.detail.get("log_found")),
            "tempo_evidence_id": tempo.evidence_id,
            "loki_evidence_id": loki.evidence_id,
        },
    )


def utc_now() -> datetime:
    """The current instant. Collectors may read a clock; the evaluator may not."""
    return datetime.now(UTC)
