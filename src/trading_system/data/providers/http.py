"""Minimal, injectable HTTP retrieval for free web-based providers.

Deliberately built on ``urllib`` from the standard library rather than adding a
dependency: the two providers that need it (SEC filings and SEC fundamentals)
make simple GET requests to a JSON API, and a new third-party package would buy
nothing.

The important property is that :class:`HttpFetcher` is a protocol. Ordinary
tests inject a fixture fetcher and make no network call at all, which is what
keeps ``pytest`` hermetic while still exercising the real parsing code.

Every request is bounded by an explicit timeout, sends a descriptive
User-Agent — the SEC blocks requests without one — and never logs or echoes a
header value, since headers are where credentials live in general even though
these particular requests carry none.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from trading_system.data.providers.base import (
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

__all__ = ["HttpFetcher", "HttpResponse", "StaticHttpFetcher", "UrllibHttpFetcher"]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A retrieved HTTP body, with just enough metadata to be auditable."""

    url: str
    status: int
    body: str
    #: Response headers, lower-cased. Used for ``Last-Modified``, which is the
    #: source's own timestamp for the payload.
    headers: Mapping[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(f"{self.url} did not return valid JSON: {exc}") from exc


@runtime_checkable
class HttpFetcher(Protocol):
    """Anything that can fetch a URL. Injected so tests need no network."""

    def get(self, url: str, *, timeout_seconds: float) -> HttpResponse: ...


class UrllibHttpFetcher:
    """Real HTTP GET over the standard library."""

    def __init__(self, *, user_agent: str) -> None:
        if not user_agent.strip():
            raise ValueError(
                "a descriptive User-Agent is required: the SEC rejects anonymous "
                "requests, and an unidentified crawler is bad manners besides"
            )
        self._user_agent = user_agent

    def get(self, url: str, *, timeout_seconds: float) -> HttpResponse:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self._user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            method="GET",
        )
        if not url.lower().startswith("https://"):
            raise ProviderUnavailableError(f"refusing a non-HTTPS request to {url}")

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
                headers = {k.lower(): v for k, v in response.headers.items()}
                return HttpResponse(
                    url=url, status=int(response.status), body=body, headers=headers
                )
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503):
                raise ProviderRateLimitError(
                    f"{url} rate-limited the request (HTTP {exc.code})"
                ) from exc
            raise ProviderUnavailableError(f"{url} returned HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise ProviderTimeoutError(f"{url} did not respond within {timeout_seconds}s") from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailableError(f"could not reach {url}: {exc.reason}") from exc
        except OSError as exc:
            raise ProviderUnavailableError(f"could not reach {url}: {exc}") from exc


class StaticHttpFetcher:
    """A fetcher backed by a fixed URL-to-body mapping.

    For tests and for replaying recorded fixtures. An unknown URL raises
    ``ProviderUnavailableError`` rather than returning an empty body, so a test
    that forgot to record a response fails instead of silently asserting
    against nothing.
    """

    def __init__(
        self,
        responses: Mapping[str, str],
        *,
        headers: Mapping[str, Mapping[str, str]] | None = None,
        status: int = 200,
    ) -> None:
        self._responses = dict(responses)
        self._headers = {url: dict(values) for url, values in (headers or {}).items()}
        self._status = status
        self.requested_urls: list[str] = []

    def get(self, url: str, *, timeout_seconds: float) -> HttpResponse:
        self.requested_urls.append(url)
        if url not in self._responses:
            raise ProviderUnavailableError(f"no recorded response for {url}")
        return HttpResponse(
            url=url,
            status=self._status,
            body=self._responses[url],
            headers=self._headers.get(url, {}),
        )
