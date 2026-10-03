"""HTTP for market_index (requests): retries with backoff, request spacing, and block detection."""
from __future__ import annotations

from decimal import Decimal
import random
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TIMEOUT = (10, 30)  # connect, read seconds

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class BlockedError(Exception):
    """The server or its firewall refused us; continuing would make it worse."""


class Throttle:
    """Keeps at least `delay` (+ random jitter) seconds between requests."""

    def __init__(self, delay: float, jitter: float) -> None:
        self.delay = delay
        self.jitter = jitter
        self._last = 0.0

    def wait(self) -> None:
        pause = self.delay + random.uniform(0, self.jitter) - (time.monotonic() - self._last)
        if pause > 0:
            time.sleep(pause)
        self._last = time.monotonic()


def build_session(headers: dict[str, str]) -> requests.Session:
    retry = Retry(
        total=4,
        backoff_factor=2.0,  # 2s, 4s, 8s, 16s between attempts
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_USER_AGENT, **headers})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_json(session: requests.Session, url: str, params: dict, what: str):
    """GET a JSON endpoint, raising BlockedError on firewall/WAF responses.
    Decimal numbers are read exactly as written (no float rounding)."""
    resp = session.get(url, params=params, timeout=TIMEOUT)
    if resp.status_code in (401, 403):
        raise BlockedError(f"HTTP {resp.status_code} for {what}")
    resp.raise_for_status()
    if not resp.content:
        raise ValueError(f"empty response for {what} (unknown code?)")
    # A WAF block page is usually served as HTML with a 200 status.
    if "json" not in resp.headers.get("Content-Type", "") and resp.text.lstrip().startswith("<"):
        raise BlockedError(f"HTML page instead of JSON for {what}: {resp.text[:120]!r}")
    return resp.json(parse_float=Decimal)
