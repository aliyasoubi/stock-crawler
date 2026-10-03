"""Client for the JSON backend behind https://evds3.tcmb.gov.tr.

EVDS3 is a React single-page app; its HTML contains no data. The browser pulls
everything from ``/igmevdsms-dis`` - the same endpoints are used here, so no
HTML scraping, headless browser or API key is needed.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .fields import Agg, Freq
from .periods import parse_evds_date

log = logging.getLogger(__name__)

BASE_URL = "https://evds3.tcmb.gov.tr/igmevdsms-dis"
USER_AGENT = "sovereign-crawler/1.0 (macro research; python-requests)"
TIMEOUT = (10, 90)  # connect, read (seconds)


class EvdsError(RuntimeError):
    pass


class SeriesNotFoundError(EvdsError):
    pass


class EvdsClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        verify: bool | str = True,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval  # polite gap between requests (seconds)
        self.verify = verify
        self.session = session or self._build_session()
        self._last_call = 0.0
        self._catalog: dict[str, dict[str, dict[str, Any]]] = {}

    @staticmethod
    def _build_session() -> requests.Session:
        retry = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),  # /fe is a read-only POST
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        return session

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

        resp = self.session.request(method, f"{self.base_url}{path}", timeout=TIMEOUT, verify=self.verify, **kwargs)
        if resp.status_code >= 400:
            raise EvdsError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]!r}")
        try:
            return resp.json()
        except ValueError as exc:
            raise EvdsError(f"{method} {path} returned non-JSON: {resp.text[:200]!r}") from exc

    def series_catalog(self, datagroup: str) -> dict[str, dict[str, Any]]:
        """Series metadata of a datagroup, keyed by series code (cached)."""
        if datagroup not in self._catalog:
            rows = self._request("GET", f"/serieList/fe/type=json&code={datagroup}")
            if not isinstance(rows, list):
                raise EvdsError(f"Unexpected catalogue payload for {datagroup!r}")
            self._catalog[datagroup] = {row["SERIE_CODE"]: row for row in rows}
        return self._catalog[datagroup]

    def assert_series_exist(self, datagroup: str, codes: list[str]) -> None:
        # /fe answers an unknown code with a bare HTTP 500, indistinguishable from
        # an outage, so validate against the catalogue first.
        catalog = self.series_catalog(datagroup)
        missing = [c for c in codes if c not in catalog]
        if missing:
            raise SeriesNotFoundError(
                f"Series {missing} not found in EVDS datagroup {datagroup!r}; "
                "the code may have been renamed or rebased - check the datagroup on evds3.tcmb.gov.tr"
            )

    def fetch_series(
        self, code: str, freq: Freq, start: date, end: date, agg: Agg = Agg.AVG
    ) -> dict[date, Decimal]:
        """Observations at ``freq`` between ``start`` and ``end``, keyed by period start date."""
        out: dict[date, Decimal] = {}
        # Fetch daily series one calendar year at a time to keep responses small.
        chunk_years = 1 if freq.months is None else 100
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, date(chunk_start.year + chunk_years - 1, 12, 31))
            out.update(self._fetch_chunk(code, freq, chunk_start, chunk_end, agg))
            chunk_start = date(chunk_end.year + 1, 1, 1)
        return out

    def _fetch_chunk(self, code: str, freq: Freq, start: date, end: date, agg: Agg) -> dict[date, Decimal]:
        payload = {
            "type": "json",
            "series": code,
            "aggregationTypes": agg.value,
            "formulas": "0",  # level (no % change etc.)
            "startDate": start.strftime("%d-%m-%Y"),
            "endDate": end.strftime("%d-%m-%Y"),
            "frequency": str(int(freq)),
            "decimalSeperator": ".",
            "decimal": "6",
            "dateFormat": "0",
            "lang": "en",
            "yon": "",
            "sira": "",
            "ozelFormuller": [],
            "groupSeperator": False,  # no thousands separators in numbers
            "isRaporSayfasi": False,
        }
        body = self._request("POST", "/fe", json=payload)
        items = body.get("items") if isinstance(body, dict) else None
        if items is None:
            raise EvdsError(f"/fe response for {code} has no 'items': {str(body)[:200]!r}")
        total = body.get("totalCount")
        if total is not None and total != len(items):
            raise EvdsError(f"/fe returned {len(items)} of {total} rows for {code}; response truncated")

        key = code.replace(".", "_")
        if items and all(key not in item for item in items):
            raise SeriesNotFoundError(f"Column {key!r} missing from /fe response")

        out: dict[date, Decimal] = {}
        for item in items:
            raw = item.get(key)
            if raw in (None, ""):
                continue
            try:
                out[parse_evds_date(item["Tarih"], freq)] = Decimal(str(raw))
            except (InvalidOperation, ValueError, KeyError) as exc:
                raise EvdsError(f"Unparseable observation for {code}: {item!r}") from exc
        log.debug("%s %s..%s: %d observations", code, start, end, len(out))
        return out
