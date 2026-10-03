"""Sequential HTTPS client shared by the crawlers: adaptive throttle, retries, content validation.

Python 3.10+ standard library only; `curl` is used as a fallback and for proxies.
"""
from __future__ import annotations

from email.utils import parsedate_to_datetime
import gzip
import http.client
import logging
from pathlib import Path
import random
import shutil
import subprocess
import ssl
import tempfile
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit

USER_AGENT = "TurkeyCompanyDirectory/2.0 (public company directory)"
# Statuses that mean "slow down / try later". 403 is how many web firewalls throttle.
RETRYABLE_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
LOG = logging.getLogger("http_client")


class FetchError(RuntimeError):
    """A URL could not be fetched after all retries."""


class NotFound(FetchError):
    """HTTP 404/410: the page does not exist; retrying will not help."""


class Throttle:
    """Adaptive gap between requests.

    Starts at `base` seconds with random jitter so requests are not perfectly periodic.
    Every sign of push-back (429/403/5xx, dropped connection, empty page) doubles the gap up to
    `maximum`; each run of `recover_after` successes shrinks it by 30% again.
    A hard limit (HTTP 429/403) also teaches a floor: the gap never returns below 1.5x the
    rate that was running when the limit hit, so the crawl settles just under the server's quota.
    """

    def __init__(self, base: float, maximum: float = 60.0, jitter: float = 0.3, recover_after: int = 5) -> None:
        self.base = self.floor = self.interval = self._last_good = base
        self.maximum = max(maximum, base)
        self.jitter = jitter
        self.recover_after = recover_after
        self._streak = 0
        self._next = 0.0

    def wait(self) -> None:
        delay = self._next - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next = time.monotonic() + self.interval * random.uniform(1 - self.jitter, 1 + self.jitter)

    def success(self) -> None:
        self._streak += 1
        if self._streak >= self.recover_after:
            self._streak = 0
            self._last_good = self.interval
            if self.interval > self.floor:
                self.interval = max(self.floor, self.interval * 0.7)
                LOG.info("Server responding normally; request gap now %.1fs", self.interval)

    def slow_down(self, reason: str, hard_limit: bool = False) -> None:
        self._streak = 0
        if hard_limit:
            floor = min(self.maximum / 2, max(self.floor, self._last_good * 1.5))
            if floor > self.floor:
                LOG.warning("Rate limit hit at a %.1fs gap; will keep at least %.1fs between requests",
                            self._last_good, floor)
                self.floor = floor
        new = min(self.maximum, self.interval * 2)
        if new > self.interval:
            LOG.warning("Slowing down: request gap %.1fs -> %.1fs (%s)", self.interval, new, reason)
        self.interval = new


class HttpClient:
    """Sequential GET client: adaptive throttle, retries, content validation.

    The Python backend keeps one HTTPS connection per host open (keep-alive), so KAP sees one
    TLS handshake instead of one per page. TLS certificates are always verified.
    """

    def __init__(self, backend: str = "auto", proxy: str | None = None, interval: float = 2.0,
                 max_interval: float = 60.0, attempts: int = 4, timeout: float = 45,
                 ca_bundle: str | None = None) -> None:
        self.backend = "curl" if proxy else backend
        self.proxy = proxy
        self.throttle = Throttle(interval, max_interval)
        self.attempts = attempts
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=ca_bundle)
        self._connections: dict[str, http.client.HTTPSConnection] = {}
        self._python_worked = False
        if self.backend == "curl" and not shutil.which("curl"):
            raise RuntimeError("curl is required for --proxy / --fetch-backend curl")

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()

    def get(self, url: str, validate: Callable[[bytes], bool] | None = None) -> bytes:
        for attempt in range(1, self.attempts + 1):
            self.throttle.wait()
            retry_after = status = None
            try:
                status, body, retry_after = self._request(url)
            except FetchError as exc:
                problem = str(exc)
            else:
                if status == 200 and (validate is None or validate(body)):
                    self.throttle.success()
                    return body
                if status in (404, 410):
                    raise NotFound(f"{url}: HTTP {status}")
                if status == 200:
                    problem = f"empty or unexpected page ({len(body)} bytes)"
                elif status in RETRYABLE_STATUS:
                    problem = f"HTTP {status}"
                else:
                    raise FetchError(f"{url}: HTTP {status}")
            self.throttle.slow_down(problem, hard_limit=status in (403, 429))
            if attempt == self.attempts:
                raise FetchError(f"{url}: {problem} (after {attempt} attempts)")
            wait = min(retry_after if retry_after else 5 * 2 ** (attempt - 1), 300)
            LOG.warning("%s: %s; retry %s/%s in %.0fs", url, problem, attempt, self.attempts - 1, wait)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def _request(self, url: str) -> tuple[int, bytes, float | None]:
        if self.backend == "curl":
            return self._curl(url)
        try:
            result = self._python(url)
            self._python_worked = True
            return result
        except (OSError, http.client.HTTPException) as exc:
            # Fall back to curl only if Python never managed to connect (e.g. local CA/TLS setup);
            # once it has worked, an error is a network or server problem and is retried as such.
            if self.backend == "auto" and not self._python_worked and shutil.which("curl"):
                LOG.warning("Python HTTPS client cannot connect (%s); switching to curl "
                            "(no keep-alive; see --ca-bundle)", exc)
                self.backend = "curl"
                return self._curl(url)
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    def _python(self, url: str) -> tuple[int, bytes, float | None]:
        for _ in range(5):  # follow up to 5 redirects
            parts = urlsplit(url)
            if parts.scheme != "https":
                raise FetchError(f"Refusing non-HTTPS URL {url}")
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/zip,*/*",
                       "Accept-Encoding": "gzip", "Connection": "keep-alive"}
            reused = parts.netloc in self._connections
            try:
                response, body = self._send(parts.netloc, target, headers)
            except (OSError, http.client.HTTPException):
                self._drop(parts.netloc)
                if not reused:
                    raise
                # The server closed an idle keep-alive connection; retry once on a fresh one.
                response, body = self._send(parts.netloc, target, headers)
            if response.will_close:
                self._drop(parts.netloc)
            if response.getheader("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            location = response.getheader("Location")
            if response.status in REDIRECT_STATUS and location:
                url = urljoin(url, location)
                continue
            return response.status, body, _retry_after(response.getheader("Retry-After"))
        raise FetchError(f"{url}: too many redirects")

    def _send(self, host: str, target: str, headers: dict[str, str]) -> tuple[http.client.HTTPResponse, bytes]:
        connection = self._connections.get(host)
        if connection is None:
            connection = http.client.HTTPSConnection(host, timeout=self.timeout, context=self.ssl_context)
            self._connections[host] = connection
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        return response, response.read()

    def _drop(self, host: str) -> None:
        connection = self._connections.pop(host, None)
        if connection:
            connection.close()

    def _curl(self, url: str) -> tuple[int, bytes, float | None]:
        with tempfile.NamedTemporaryFile(prefix="curl-headers-") as header_file:
            command = ["curl", "--location", "--silent", "--show-error", "--compressed",
                       "--connect-timeout", "15", "--max-time", str(int(self.timeout)),
                       "--user-agent", USER_AGENT, "--dump-header", header_file.name,
                       "--write-out", "\n%{http_code}"]
            if self.proxy:
                command += ["--proxy", self.proxy, "--noproxy", ""]
            try:
                result = subprocess.run(command + [url], capture_output=True, timeout=self.timeout + 15)
            except subprocess.TimeoutExpired as exc:
                raise FetchError("curl timed out") from exc
            headers = Path(header_file.name).read_text(encoding="latin-1")
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise FetchError(f"curl exit {result.returncode}: {detail}")
        body, _, status = result.stdout.rpartition(b"\n")
        if not status.isdigit():
            raise FetchError("curl returned no HTTP status")
        retry_after = None
        for line in headers.splitlines():  # with redirects, the last response's header wins
            name, _, value = line.partition(":")
            if name.strip().lower() == "retry-after":
                retry_after = _retry_after(value.strip())
        return int(status), body, retry_after


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    if value.strip().isdigit():
        return float(value)
    try:
        return max(parsedate_to_datetime(value).timestamp() - time.time(), 0)
    except (TypeError, ValueError):
        return None
