"""Cautious HTTP retrieval: pacing, global budget, bounded retries, cooldowns, access stops.

The client is source-agnostic. kap.py decides which URLs to request; this module decides
whether and when a request may be sent, and turns source stop signals into exceptions that
end the run cleanly. Nothing here ever evades restrictions.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Settings
from .storage import Clock, StateStore, utcnow

Sleeper = Callable[[float], None]

RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5

# Conservative markers for a security challenge served with HTTP 200.
_CHALLENGE_MARKERS = (
    re.compile(rb"cf-challenge|cf_chl_|challenge-platform|jschl", re.I),
    re.compile(rb"<title>\s*(just a moment|attention required|access denied)", re.I),
    re.compile(rb"g-recaptcha|h-captcha|hcaptcha\.com|recaptcha/api", re.I),
    re.compile(rb"incapsula|_Incapsula_Resource|imperva", re.I),
)


class FetchError(RuntimeError):
    """Base class for retrieval failures that end the current request."""


class BudgetExhausted(FetchError):
    """The per-run HTTP attempt budget is used up; stop the run and list pending work."""


class HostCoolingDown(FetchError):
    def __init__(self, host: str, until: datetime, reason: str | None) -> None:
        super().__init__(f"host {host} is cooling down until {until.isoformat()} ({reason})")
        self.host, self.until, self.reason = host, until, reason


class HostThrottled(FetchError):
    """429 persisted after bounded retries; a cooldown has been recorded."""


class AccessBlocked(FetchError):
    """401/403 or a security challenge: stop requests to the host until resolved."""


class SourceHTTPError(FetchError):
    def __init__(self, url: str, status: int | None, detail: str) -> None:
        super().__init__(f"{detail} ({status}) for {url}")
        self.url, self.status = url, status


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str | None
    headers: dict[str, str] = field(default_factory=dict)
    not_modified: bool = False

    @property
    def validators(self) -> dict[str, str]:
        found = {}
        if "etag" in self.headers:
            found["etag"] = self.headers["etag"]
        if "last-modified" in self.headers:
            found["last_modified"] = self.headers["last-modified"]
        return found


def looks_like_challenge(content_type: str | None, body: bytes) -> bool:
    if content_type and "html" not in content_type.lower():
        return False
    head = body[:65536]
    return any(marker.search(head) for marker in _CHALLENGE_MARKERS)


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Retry-After as delay seconds or HTTP-date (RFC 9110). None when absent/unparseable."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - now).total_seconds())


class PacedClient:
    """One in-flight request, per-host pacing, and one global attempt budget per run."""

    def __init__(
        self,
        client: httpx.Client,
        settings: Settings,
        state: StateStore,
        *,
        allowed_hosts: set[str],
        clock: Clock = utcnow,
        sleeper: Sleeper = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._state = state
        self._allowed_hosts = {host.lower() for host in allowed_hosts}
        self._clock = clock
        self._sleep = sleeper
        self._rng = rng or random.Random()
        self._next_allowed: dict[str, datetime] = {}
        self.attempts = 0
        self.budget = settings.max_requests_per_run

    @property
    def remaining_budget(self) -> int:
        return max(0, self.budget - self.attempts)

    def get(self, url: str, *, validators: dict[str, str] | None = None, accept: str | None = None) -> FetchResult:
        headers = {"User-Agent": self._settings.http_user_agent}
        if accept:
            headers["Accept"] = accept
        if validators:
            if validators.get("etag"):
                headers["If-None-Match"] = validators["etag"]
            if validators.get("last_modified"):
                headers["If-Modified-Since"] = validators["last_modified"]
        redirects = 0
        while True:
            host = self._check_host(url)
            self._check_cooldown(host)
            response = self._request_with_retries(url, host, headers)
            if response.status_code in REDIRECT_STATUSES:
                redirects += 1
                location = response.headers.get("location")
                if not location or redirects > MAX_REDIRECTS:
                    raise SourceHTTPError(url, response.status_code, "redirect without location or too many redirects")
                url = urljoin(url, location)
                continue
            content_type = response.headers.get("content-type")
            if response.status_code == 304:
                return FetchResult(url, 304, b"", content_type, dict(response.headers), not_modified=True)
            return FetchResult(url, response.status_code, response.content, content_type, dict(response.headers))

    # -- internals ---------------------------------------------------------------------

    def _check_host(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme != "https" or host not in self._allowed_hosts:
            raise SourceHTTPError(url, None, "refusing request to unverified host or non-https URL")
        return host

    def _check_cooldown(self, host: str) -> None:
        until, reason = self._state.host_cooldown(host)
        if until is not None:
            raise HostCoolingDown(host, until, reason)

    def _consume_budget(self) -> None:
        if self.attempts >= self.budget:
            raise BudgetExhausted(f"request budget of {self.budget} attempts exhausted")
        self.attempts += 1

    def _pace(self, host: str) -> None:
        next_allowed = self._next_allowed.get(host)
        if next_allowed is not None:
            wait = (next_allowed - self._clock()).total_seconds()
            if wait > 0:
                self._sleep(wait)

    def _schedule_next(self, host: str) -> None:
        delay = self._rng.uniform(self._settings.request_delay_min_seconds, self._settings.request_delay_max_seconds)
        self._next_allowed[host] = self._clock() + timedelta(seconds=delay)

    def _backoff(self, attempt: int) -> float:
        schedule = self._settings.retry_backoff_seconds or (10.0,)
        return schedule[min(attempt, len(schedule) - 1)]

    def _request_with_retries(self, url: str, host: str, headers: dict[str, str]) -> httpx.Response:
        retries = 0
        while True:
            self._pace(host)
            self._consume_budget()
            try:
                response = self._client.get(
                    url, headers=headers, timeout=self._settings.request_timeout_seconds, follow_redirects=False
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                self._schedule_next(host)
                if retries < self._settings.max_retries:
                    self._sleep(self._backoff(retries))
                    retries += 1
                    continue
                raise SourceHTTPError(url, None, f"transport failure after {retries} retries: {exc.__class__.__name__}") from exc
            self._schedule_next(host)
            status = response.status_code
            content_type = response.headers.get("content-type")

            if status in (401, 403):
                self._state.set_host_blocked(host, f"HTTP {status} for {url}")
                raise AccessBlocked(f"HTTP {status} from {host}; stopping requests to this host")
            if status == 200 and looks_like_challenge(content_type, response.content):
                self._state.set_host_blocked(host, f"security challenge returned with 200 for {url}")
                raise AccessBlocked(f"security challenge page from {host}; stopping requests to this host")
            if status == 429:
                now = self._clock()
                delay = parse_retry_after(response.headers.get("retry-after"), now=now)
                delay = self._settings.retry_after_fallback_seconds if delay is None else delay
                if retries < self._settings.max_retries:
                    self._sleep(delay)
                    retries += 1
                    continue
                cooldown = max(delay, self._settings.throttle_cooldown_seconds)
                self._state.set_host_cooldown(host, now + timedelta(seconds=cooldown), f"HTTP 429 persisted for {url}")
                raise HostThrottled(f"HTTP 429 persisted from {host}; cooldown {cooldown:.0f}s recorded")
            if status in RETRYABLE_STATUSES:
                if retries < self._settings.max_retries:
                    delay = parse_retry_after(response.headers.get("retry-after"), now=self._clock())
                    self._sleep(self._backoff(retries) if delay is None else delay)
                    retries += 1
                    continue
                raise SourceHTTPError(url, status, f"server error persisted after {retries} retries")
            if status >= 400 and status != 304:
                raise SourceHTTPError(url, status, "client error")
            return response
