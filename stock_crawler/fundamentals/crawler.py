"""
KAP (Public Disclosure Platform, kap.org.tr) fundamentals crawler.

Fetches quarterly financial-statement items for every listed Turkish company
(BIST, KAP member type "IGS") from KAP's "Financial Statement Item Search"
page (https://www.kap.org.tr/en/kalem-karsilastirma) and writes a CSV shaped
like the `CompanyFundamental` table.

How it works
------------
1. GET  /en/api/company/items/IGS/A                    -> listed companies (stock code, oid, KAP code)
2. GET  /en/api/analysis/companies-by-sector/{SECTOR}  -> which companies file which statement type
3. POST /en/api/export/compareItems                    -> XLSX export (same file as the website's
                                                          "Download" button), max 10 companies,
                                                          2 years, 10 items per request
Each raw XLSX response is cached on disk, so an interrupted run resumes where
it stopped and re-runs only hit the network for stale/recent data.

Usage
-----
    # the last --years years (from config/config.toml), all listed companies
    python -m stock_crawler fundamentals

    # incremental update: previous + current year, merged into the existing CSV
    python -m stock_crawler fundamentals --update

    # a few symbols / custom years
    python -m stock_crawler fundamentals --symbols ASELS,THYAO,GARAN --start 2023-01-01

Outputs (in --out-dir, default output/fundamentals)
    company_fundamental.csv      one row per (CompanyId, FiscalYear, FiscalQuarter), table columns
    company_fundamental_raw.csv  long format: every fetched item with its KAP metadata
    companies.csv                CompanyId <-> stock code / name / KAP oid / statement sector

Notes on the data
-----------------
* Values are copied as KAP publishes them, in the statement's presentation unit:
  the PresentationCurrency column says which ("TL", "1000TL", "USD", ...). Only
  KAP's number format is read ("1.234.567" -> 1234567, "-1.234,56" -> -1234.56).
* Cumulative (year-to-date) income-statement values, exactly as KAP publishes
  them: Q2 = 6 months, Q3 = 9 months, Q4 = full year.
* The item-search endpoint does not publish EBITDA, debt split, cash (except
  the finance sector), free cash flow, EPS or share count; those columns are
  left empty here and filled by `fundamental_reports` from the full reports.
  See FIELD_MAP to change how columns are derived.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import io
import json
import logging
import random
import re
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Iterator

import httpx
import openpyxl

log = logging.getLogger("kap")
warnings.filterwarnings("ignore", message="Workbook contains no default style", module="openpyxl")

BASE_URL = "https://www.kap.org.tr"  # --base-url; the paths below are appended to it
LANG = "en"
PAGE_PATH = f"/{LANG}/kalem-karsilastirma"
LISTED_COMPANIES_PATH = f"/{LANG}/api/company/items/IGS/A"
SECTOR_COMPANIES_PATH = f"/{LANG}/api/analysis/companies-by-sector/{{sector}}"
SECTOR_ITEMS_PATH = f"/{LANG}/api/analysis/compare-items-by-sector/{{sector}}"
EXPORT_PATH = f"/{LANG}/api/export/compareItems"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Limits enforced by the website's own form; staying inside them keeps our
# traffic indistinguishable from normal use of the page.
MAX_COMPANIES_PER_REQUEST = 10
MAX_YEARS_PER_REQUEST = 2
MAX_ITEMS_PER_REQUEST = 10
FIRST_YEAR = 2016  # earliest year offered by the page

TABLE_COLUMNS = [
    "CompanyId", "FiscalYear", "FiscalQuarter", "PeriodEndDate", "PublishDate",
    "Revenue", "OperatingIncome", "NetIncome", "Ebitda", "TotalAssets",
    "TotalLiabilities", "Equity", "TotalDebtShort", "TotalDebtLong",
    "CashAndEquivalents", "CurrentLiabilities", "NonCurrentLiabilities",
    "FreeCashFlow", "Eps", "SharesOutstanding",
]
META_COLUMNS = [
    "StockCode", "CompanyName", "StatementNature",
    "SectoralStatementType", "NotificationId",
]
UNIT_COLUMN = "PresentationCurrency"  # always written after TABLE_COLUMNS: the unit of the values

# --------------------------------------------------------------------------- #
# Item ids (XBRL concepts used by KAP) and how they map to the table columns.
# --------------------------------------------------------------------------- #
REVENUE = "ifrs-full_Revenue"
REVENUE_FIN = "kap-fr_RevenueFromFinanceSectorOperations"
HOLDING_REVENUE = "kap-fr_HoldingRevenue"
BANK_GROSS_OPERATING = "kap-fr_GrossProfitLossFromOperatingActivitiesForBankingSector"
FIN_OPERATING_INCOME = "kap-fr_OperatingIncome"
FIN_OPERATING_PROFIT = "kap-fr_OperatingProfitLoss"
FIN_CASH = "kap-fr_CashAndCashBalancesAtCentralBanks"
INS_NONLIFE_INCOME = "kap-fr_NonlifeTechnicalIncome"
INS_LIFE_INCOME = "kap-fr_LifeTechnicalIncome"
INS_PENSION_INCOME = "kap-fr_PensionBusinessTechnicalIncome"
OPERATING = "ifrs-full_ProfitLossFromOperatingActivities"
NET_PARENT = "ifrs-full_ProfitLossAttributableToOwnersOfParent"
NET = "ifrs-full_ProfitLoss"
ASSETS = "ifrs-full_Assets"
LIABILITIES = "ifrs-full_Liabilities"
EQUITY = "ifrs-full_Equity"
EQUITY_AND_LIAB = "ifrs-full_EquityAndLiabilities"
CURRENT_LIAB = "ifrs-full_CurrentLiabilities"
NONCURRENT_LIAB = "ifrs-full_NoncurrentLiabilities"

Values = dict[str, "Decimal | None"]


def first(v: Values, *ids: str) -> Decimal | None:
    return next((v[i] for i in ids if v.get(i) is not None), None)


def total(v: Values, *ids: str) -> Decimal | None:
    present = [v[i] for i in ids if v.get(i) is not None]
    return sum(present, Decimal(0)) if present else None


def minus(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return None if a is None or b is None else a - b


@dataclass(frozen=True)
class SectorSpec:
    items: tuple[str, ...]
    fields: dict[str, Callable[[Values], Decimal | None]]


_COMMON_NET = {"NetIncome": lambda v: first(v, NET_PARENT, NET)}
_LIAB_FROM_BALANCE = lambda v: minus(first(v, EQUITY_AND_LIAB), first(v, EQUITY))  # noqa: E731

FIELD_MAP: dict[str, SectorSpec] = {
    # Industrial/commercial companies (the bulk of BIST).
    "GENERAL": SectorSpec(
        items=(REVENUE, REVENUE_FIN, OPERATING, NET_PARENT, NET, ASSETS,
               LIABILITIES, EQUITY, CURRENT_LIAB, NONCURRENT_LIAB),
        fields={
            "Revenue": lambda v: total(v, REVENUE, REVENUE_FIN),
            "OperatingIncome": lambda v: first(v, OPERATING),
            **_COMMON_NET,
            "TotalAssets": lambda v: first(v, ASSETS),
            "TotalLiabilities": lambda v: first(v, LIABILITIES) or total(v, CURRENT_LIAB, NONCURRENT_LIAB),
            "Equity": lambda v: first(v, EQUITY),
            "CurrentLiabilities": lambda v: first(v, CURRENT_LIAB),
            "NonCurrentLiabilities": lambda v: first(v, NONCURRENT_LIAB),
        },
    ),
    "HOLDING": SectorSpec(
        items=(HOLDING_REVENUE, REVENUE, REVENUE_FIN, OPERATING, NET_PARENT, NET,
               ASSETS, LIABILITIES, EQUITY, CURRENT_LIAB, NONCURRENT_LIAB),
        fields={
            "Revenue": lambda v: first(v, HOLDING_REVENUE) or total(v, REVENUE, REVENUE_FIN),
            "OperatingIncome": lambda v: first(v, OPERATING),
            **_COMMON_NET,
            "TotalAssets": lambda v: first(v, ASSETS),
            "TotalLiabilities": lambda v: first(v, LIABILITIES) or total(v, CURRENT_LIAB, NONCURRENT_LIAB),
            "Equity": lambda v: first(v, EQUITY),
            "CurrentLiabilities": lambda v: first(v, CURRENT_LIAB),
            "NonCurrentLiabilities": lambda v: first(v, NONCURRENT_LIAB),
        },
    ),
    # Banks: no revenue line -> gross operating income; unclassified balance sheet.
    **{
        sector: SectorSpec(
            items=(BANK_GROSS_OPERATING, OPERATING, NET_PARENT, NET, ASSETS, EQUITY, EQUITY_AND_LIAB),
            fields={
                "Revenue": lambda v: first(v, BANK_GROSS_OPERATING),
                "OperatingIncome": lambda v: first(v, OPERATING),
                **_COMMON_NET,
                "TotalAssets": lambda v: first(v, ASSETS),
                "TotalLiabilities": _LIAB_FROM_BALANCE,
                "Equity": lambda v: first(v, EQUITY),
            },
        )
        for sector in ("BANKS", "PAR-BANKS")
    },
    # Insurance: revenue = technical income (non-life + life + pension).
    "INSURANCE": SectorSpec(
        items=(INS_NONLIFE_INCOME, INS_LIFE_INCOME, INS_PENSION_INCOME, NET_PARENT, NET,
               ASSETS, EQUITY, EQUITY_AND_LIAB, CURRENT_LIAB, NONCURRENT_LIAB),
        fields={
            "Revenue": lambda v: total(v, INS_NONLIFE_INCOME, INS_LIFE_INCOME, INS_PENSION_INCOME),
            **_COMMON_NET,
            "TotalAssets": lambda v: first(v, ASSETS),
            "TotalLiabilities": _LIAB_FROM_BALANCE,
            "Equity": lambda v: first(v, EQUITY),
            "CurrentLiabilities": lambda v: first(v, CURRENT_LIAB),
            "NonCurrentLiabilities": lambda v: first(v, NONCURRENT_LIAB),
        },
    ),
    # Leasing / factoring / financing companies.
    "FINANCE": SectorSpec(
        items=(FIN_OPERATING_INCOME, FIN_OPERATING_PROFIT, NET_PARENT, NET, ASSETS,
               EQUITY, EQUITY_AND_LIAB, FIN_CASH),
        fields={
            "Revenue": lambda v: first(v, FIN_OPERATING_INCOME),
            "OperatingIncome": lambda v: first(v, FIN_OPERATING_PROFIT),
            **_COMMON_NET,
            "TotalAssets": lambda v: first(v, ASSETS),
            "TotalLiabilities": _LIAB_FROM_BALANCE,
            "Equity": lambda v: first(v, EQUITY),
            "CashAndEquivalents": lambda v: first(v, FIN_CASH),
        },
    ),
}

# Companies whose fiscal year does not start in January (KAP "Year" = the year the
# fiscal year starts in; e.g. GSRAY 2025 Q1 = Jun-Aug 2025, Q4 ends May 2026).
FISCAL_YEAR_START_MONTH = {"GSRAY": 6, "FENER": 6, "BJKAS": 6, "TSPOR": 6}


def period_end(company: "Company", year: int, quarter: int) -> dt.date:
    start = next((FISCAL_YEAR_START_MONTH[s] for s in company.symbols if s in FISCAL_YEAR_START_MONTH), 1)
    months = year * 12 + (start - 1) + 3 * quarter  # first day of the month after period end
    return dt.date(months // 12, months % 12 + 1, 1) - dt.timedelta(days=1)


# --------------------------------------------------------------------------- #
# Polite HTTP client
# --------------------------------------------------------------------------- #
class BlockedError(RuntimeError):
    """The site answered with a WAF / error page instead of data."""


class PoliteClient:
    """Single-connection HTTP client with jittered pacing, periodic long pauses,
    exponential backoff (honouring Retry-After) and a circuit breaker.

    Pacing is adaptive: every refusal (429/403/disconnect/block page) doubles the
    delay between requests, and each streak of successes eases it back down."""

    MAX_SLOWDOWN = 8.0
    EASE_AFTER = 25  # successes in a row before speeding up again

    RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(self, *, base_url: str, min_delay: float, max_delay: float, long_pause_every: int,
                 long_pause: tuple[float, float], max_retries: int, user_agent: str,
                 cooldown: float = 600.0, timeout: float = 60.0):
        self.base_url = base_url
        self.page_url = base_url + PAGE_PATH
        self.min_delay, self.max_delay = min_delay, max_delay
        self.long_pause_every, self.long_pause = long_pause_every, long_pause
        self.max_retries = max_retries
        self.cooldown = cooldown
        self.user_agent, self.timeout = user_agent, timeout
        self.requests_made = 0
        self._last_request_at = 0.0
        self._consecutive_blocks = 0
        self._successes = 0
        self.slowdown = 1.0
        self._warmed_up = False
        self.client = self._new_client()

    def _new_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            # The server closes idle keep-alive connections after 5s; drop ours first
            # so a request is never sent on a connection that is being torn down.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=4.0),
            headers={
                "User-Agent": self.user_agent,
                "Accept-Language": LANG,
                "Referer": self.page_url,
            },
        )

    def _reset_session(self) -> None:
        """Start over with a new connection and no cookies, like a fresh browser visit."""
        self.client.close()
        self.client = self._new_client()
        self._warmed_up = False

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _pace(self) -> None:
        if self.requests_made and self.long_pause_every and self.requests_made % self.long_pause_every == 0:
            pause = random.uniform(*self.long_pause)
            log.info("Taking a %.0fs break after %d requests", pause, self.requests_made)
            time.sleep(pause)
        delay = random.uniform(self.min_delay, self.max_delay) * self.slowdown
        wait = delay - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def warm_up(self) -> None:
        """Open the page once like a browser would, to receive session cookies."""
        if self._warmed_up:
            return
        self._pace()
        try:
            self.client.get(self.page_url, headers={"Accept": "text/html,application/xhtml+xml"})
        except httpx.HTTPError as exc:
            log.warning("Warm-up request failed: %s", exc)
            if self.NETWORK_DOWN.search(str(exc)):
                self.wait_for_network()
        self._last_request_at = time.monotonic()
        self.requests_made += 1
        self._warmed_up = True

    NETWORK_DOWN = re.compile(r"nodename nor servname|Name or service not known|name resolution|"
                              r"Network is unreachable|No route to host|getaddrinfo failed", re.I)

    def wait_for_network(self) -> None:
        """Our own connection is down (laptop asleep, Wi-Fi lost): wait, don't blame the server."""
        log.warning("Network unavailable - waiting for it to come back")
        while True:
            time.sleep(60)
            try:
                httpx.get(self.base_url, timeout=15, headers={"User-Agent": self.user_agent})
                log.info("Network is back")
                return
            except httpx.HTTPError as exc:
                if not self.NETWORK_DOWN.search(str(exc)):
                    return

    def request(self, method: str, url: str, *, expect: str, **kwargs) -> httpx.Response:
        """expect: 'json' or 'xlsx' - used to detect block pages served with HTTP 200."""
        attempt = 0
        while True:
            self.warm_up()
            self._pace()
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                resp, error = None, str(exc) or type(exc).__name__
                if self.NETWORK_DOWN.search(error):
                    self._reset_session()
                    self.wait_for_network()
                    continue
                self._register_block(url)
            finally:
                self._last_request_at = time.monotonic()
                self.requests_made += 1

            if resp is not None:
                if resp.status_code == 200 and self._looks_valid(resp, expect):
                    self._on_success()
                    return resp
                if resp.status_code not in self.RETRY_STATUS | {200, 403}:
                    resp.raise_for_status()
                error = f"HTTP {resp.status_code}" + ("" if resp.status_code != 200 else " (unexpected body)")
                if resp.status_code in (200, 403, 429):
                    self._register_block(url)

            if attempt == self.max_retries:
                raise httpx.HTTPError(f"{method} {url} failed after {attempt + 1} attempts: {error}")
            backoff = self._backoff(attempt, resp)
            log.warning("%s %s -> %s; retrying in %.0fs (attempt %d/%d)",
                        method, url, error, backoff, attempt + 1, self.max_retries)
            time.sleep(backoff)
            attempt += 1

    @staticmethod
    def _looks_valid(resp: httpx.Response, expect: str) -> bool:
        if expect == "xlsx":
            return resp.content[:2] == b"PK"
        if expect == "json":
            return "json" in resp.headers.get("content-type", "") or resp.content[:1] in (b"[", b"{")
        return True

    def _on_success(self) -> None:
        self._consecutive_blocks = 0
        self._successes += 1
        if self.slowdown > 1.0 and self._successes % self.EASE_AFTER == 0:
            self.slowdown = max(1.0, self.slowdown * 0.75)
            log.info("Server is happy again; pacing x%.2f", self.slowdown)

    def _register_block(self, url: str) -> None:
        self._consecutive_blocks += 1
        self._successes = 0
        if self.slowdown < self.MAX_SLOWDOWN:
            self.slowdown = min(self.MAX_SLOWDOWN, self.slowdown * 2)
            log.info("Slowing down: pacing x%.2f (%.0f-%.0fs between requests)", self.slowdown,
                     self.min_delay * self.slowdown, self.max_delay * self.slowdown)
        self._reset_session()
        if self._consecutive_blocks == 3:
            log.warning("Server keeps refusing requests; cooling down for %.0f min", self.cooldown / 60)
            time.sleep(self.cooldown)
        elif self._consecutive_blocks >= 6:
            raise BlockedError(
                f"Received {self._consecutive_blocks} consecutive block/rate-limit/disconnect responses "
                f"(last: {url}). Stopping; progress is cached - re-run later, ideally with a larger --min-delay."
            )

    @staticmethod
    def _backoff(attempt: int, resp: httpx.Response | None) -> float:
        retry_after = resp.headers.get("Retry-After") if resp is not None else None
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 900.0) + random.uniform(1, 5)
        return min(60.0 * 2 ** attempt, 600.0) + random.uniform(0, 10)


# --------------------------------------------------------------------------- #
# Disk cache for raw responses
# --------------------------------------------------------------------------- #
class Cache:
    def __init__(self, root: Path, recent_ttl_hours: float, old_ttl_days: float, refresh: bool):
        self.root = root
        self.recent_ttl = recent_ttl_hours * 3600
        self.old_ttl = old_ttl_days * 86400
        self.refresh = refresh
        root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(payload: dict) -> str:
        return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def path(self, kind: str, key: str, ext: str) -> Path:
        return self.root / kind / f"{key}.{ext}"

    def fresh(self, path: Path, years: Iterable[int] = ()) -> bool:
        if self.refresh or not path.exists():
            return False
        # Older fiscal years rarely change; recent ones get new filings/corrections.
        this_year = dt.date.today().year
        ttl = self.old_ttl if years and max(years) < this_year - 1 else self.recent_ttl
        return time.time() - path.stat().st_mtime < ttl

    @staticmethod
    def write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)


# --------------------------------------------------------------------------- #
# Domain objects
# --------------------------------------------------------------------------- #
@dataclass
class Company:
    oid: str
    company_id: int
    stock_code: str
    title: str
    sector: str = ""
    aliases: set[str] = field(default_factory=set)

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.stock_code.split(",") if s.strip()]


@dataclass
class ExportRow:
    company_name: str
    notification_id: str
    publish_date: dt.date | None
    year: int
    period: int
    nature: str
    currency: str
    multiplier: Decimal
    sectoral_type: str
    values: Values


_TR_MAP = str.maketrans({"İ": "I", "I": "I", "ı": "i", "Ş": "S", "ş": "s", "Ğ": "G", "ğ": "g",
                         "Ü": "U", "ü": "u", "Ö": "O", "ö": "o", "Ç": "C", "ç": "c"})
_NAME_NOISE = re.compile(r"\b(A\s*S|ANONIM|SIRKETI|SANAYI|VE|TICARET|T\s*A\s*S)\b")


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).translate(_TR_MAP).upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = _NAME_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(label))).strip().casefold()


def parse_number(text) -> Decimal | None:
    """KAP uses Turkish formatting: '1.234.567' or '-1.234,56'."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return Decimal(str(text))
    s = str(text).strip().replace(" ", "")
    if not s or s in {"-", "--"}:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_currency(text: str) -> tuple[str, Decimal]:
    """The presentation currency as published, and its multiplier: '1000TL' -> ('1000TL', 1000)."""
    text = (text or "").strip()
    m = re.match(r"^([\d.,]*)\s*[A-Za-z]+$", text)
    mult = parse_number(m.group(1)) if m and m.group(1) else None
    return text, (mult or Decimal(1))


def parse_export(content: bytes, label_to_item: dict[str, str]) -> list[ExportRow]:
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    wb.close()
    header_idx = next((i for i, r in enumerate(rows) if r and str(r[0] or "").strip() in ("Company", "Şirket")), None)
    if header_idx is None:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[header_idx]]
    fixed = 8  # Company .. Sectoral Statement Type
    item_cols: list[tuple[int, str]] = []
    for col, label in enumerate(header[fixed:], start=fixed):
        if not label:
            continue
        item_id = label_to_item.get(normalize_label(label))
        if item_id is None:
            log.warning("Unknown item column %r - skipped", label)
            continue
        item_cols.append((col, item_id))

    out = []
    for r in rows[header_idx + 1:]:
        if not r or not r[0]:
            continue
        currency, mult = parse_currency(str(r[6] or ""))
        try:
            published = dt.datetime.strptime(str(r[2]).strip(), "%d-%m-%Y %H:%M:%S").date()
        except ValueError:
            published = None
        out.append(ExportRow(
            company_name=str(r[0]).strip(),
            notification_id=str(r[1] or "").strip(),
            publish_date=published,
            year=int(str(r[3]).strip().split(".")[0]),
            period=int(str(r[4]).strip().split(".")[0]),
            nature=str(r[5] or "").strip(),
            currency=currency,
            multiplier=mult,
            sectoral_type=str(r[7] or "").strip(),
            values={item: parse_number(r[col]) if col < len(r) else None for col, item in item_cols},
        ))
    return out


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
def chunked(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def newest_first(years: list[int]) -> list[list[int]]:
    """Request year pairs from the newest back: 2016..2026 -> [2025, 2026], [2023, 2024], ..., [2016]."""
    return [sorted(pair) for pair in chunked(sorted(years, reverse=True), MAX_YEARS_PER_REQUEST)]


class KapCrawler:
    def __init__(self, http: PoliteClient, cache: Cache):
        self.http = http
        self.cache = cache
        self.base_url = http.base_url
        # Historical trade name -> company oid, learnt from single-company requests and
        # kept across runs so a renamed company only costs extra requests once.
        self.aliases_path = cache.root / "aliases.json"
        try:
            self.aliases: dict[str, str] = json.loads(self.aliases_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.aliases = {}

    def learn_aliases(self, oid: str, names: Iterable[str]) -> None:
        new = {normalize_name(n): oid for n in names}
        if any(self.aliases.get(k) != v for k, v in new.items()):
            self.aliases.update(new)
            Cache.write(self.aliases_path, json.dumps(self.aliases, ensure_ascii=False, indent=0).encode("utf-8"))

    def get_json(self, url: str, kind: str) -> list | dict:
        path = self.cache.path(kind, Cache.key({"url": url}), "json")
        if self.cache.fresh(path):
            return json.loads(path.read_text(encoding="utf-8"))
        resp = self.http.request("GET", url, expect="json", headers={"Accept": "application/json"})
        self.cache.write(path, resp.content)
        return resp.json()

    def listed_companies(self) -> dict[str, Company]:
        companies = {}
        for c in self.get_json(self.base_url + LISTED_COMPANIES_PATH, "meta"):
            if not c.get("stockCode") or not str(c.get("companyCode", "")).isdigit():
                continue
            companies[c["mkkMemberOid"]] = Company(
                oid=c["mkkMemberOid"], company_id=int(c["companyCode"]),
                stock_code=c["stockCode"], title=c["kapMemberTitle"],
            )
        return companies

    def sector_members(self, sector: str) -> list[dict]:
        return self.get_json(self.base_url + SECTOR_COMPANIES_PATH.format(sector=sector), "meta")

    def sector_labels(self, sector: str) -> dict[str, str]:
        items = self.get_json(self.base_url + SECTOR_ITEMS_PATH.format(sector=sector), "meta")
        return {normalize_label(i["websiteLabel"]): i["itemId"] for i in items}

    def export(self, sector: str, companies: list[Company], years: list[int], items: list[str]) -> bytes:
        payload = {
            "companyType": sector,
            "mkkMemberIdList": [c.oid for c in companies],
            "mkkMemberTitleList": [c.title for c in companies],
            "yearList": [str(y) for y in years],
            "periodList": ["1", "2", "3", "4"],
            "itemIdList": items,
            "sectors": [sector],
        }
        path = self.cache.path("export", Cache.key(payload), "xlsx")
        if self.cache.fresh(path, years):
            return path.read_bytes()
        log.info("Fetching %-9s %s | %s | %d items",
                 sector, ",".join(c.symbols[0] for c in companies), "-".join(map(str, years)), len(items))
        resp = self.http.request(
            "POST", self.base_url + EXPORT_PATH, expect="xlsx",
            headers={"Accept": "*/*", "Content-Type": "application/json", "Origin": self.base_url},
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        self.cache.write(path, resp.content)
        return resp.content

    # -- matching exported rows (identified by trade name at publish date) to companies --
    def assign_rows(self, rows: list[ExportRow], batch: list[Company]) -> tuple[dict[str, list[ExportRow]], set[str]]:
        by_name: dict[str, list[ExportRow]] = {}
        for r in rows:
            by_name.setdefault(r.company_name, []).append(r)

        assigned: dict[str, list[ExportRow]] = {}
        unresolved: set[str] = set()
        candidates = {c.oid: {normalize_name(c.title), *c.aliases} for c in batch}
        brands: dict[str, list[str]] = {}
        for c in batch:
            brands.setdefault(normalize_name(c.title).split(" ")[0], []).append(c.oid)
        for name, name_rows in by_name.items():
            norm = normalize_name(name)
            known = self.aliases.get(norm)
            if known in candidates:
                assigned.setdefault(known, []).extend(name_rows)
                continue
            scores = []
            for oid, names in candidates.items():
                best = max(difflib.SequenceMatcher(None, norm, n).ratio() for n in names)
                scores.append((best, oid))
            scores.sort(reverse=True)
            top, oid = scores[0]
            runner_up = scores[1][0] if len(scores) > 1 else 0.0
            if top >= 0.97 or (top >= 0.80 and top - runner_up >= 0.10):
                assigned.setdefault(oid, []).extend(name_rows)
            else:
                unresolved.add(name)
        # Renamed but kept its brand word, e.g. "ADESE ALIŞVERİŞ ..." -> "ADESE GAYRİMENKUL ...".
        # Only if the brand is unique in the batch and that company has no rows for these periods.
        for name in sorted(unresolved):
            brand = normalize_name(name).split(" ")[0]
            owners = brands.get(brand, [])
            if len(brand) < 4 or len(owners) != 1:
                continue
            taken = {(r.year, r.period) for r in assigned.get(owners[0], [])}
            if taken.isdisjoint((r.year, r.period) for r in by_name[name]):
                assigned.setdefault(owners[0], []).extend(by_name[name])
                unresolved.discard(name)
        # One leftover name and exactly one company without rows -> it's a renamed company.
        idle = [c.oid for c in batch if c.oid not in assigned]
        if len(unresolved) == 1 and len(idle) == 1:
            assigned[idle[0]] = by_name[unresolved.pop()]
        return assigned, unresolved

    def locate_names(self, names: set[str], group: list[Company], sector: str, years: list[int],
                     items: list[str], labels: dict[str, str]) -> dict[str, set[str]]:
        """Split `group` in halves and ask KAP about one half to see which old names it owns."""
        if not names or not group:
            return {}
        if len(group) == 1:
            return {group[0].oid: set(names)}
        half, rest = group[:len(group) // 2], group[len(group) // 2:]
        present = {r.company_name for r in parse_export(self.export(sector, half, years, items), labels)} & names
        return {**self.locate_names(present, half, sector, years, items, labels),
                **self.locate_names(names - present, rest, sector, years, items, labels)}

    def fetch_sector(self, sector: str, companies: list[Company], years: list[int],
                     results: list[tuple[Company, ExportRow]]) -> None:
        """Appends to `results` batch by batch, so an interrupted run keeps what it fetched."""
        spec = FIELD_MAP[sector]
        labels = self.sector_labels(sector)
        by_oid = {c.oid: c for c in companies}
        for year_pair in chunked(years, MAX_YEARS_PER_REQUEST):
            for batch in chunked(companies, MAX_COMPANIES_PER_REQUEST):
                merged: dict[tuple, tuple[Company, ExportRow]] = {}
                for item_chunk in chunked(list(spec.items), MAX_ITEMS_PER_REQUEST):
                    rows = parse_export(self.export(sector, batch, year_pair, item_chunk), labels)
                    assigned, unresolved = self.assign_rows(rows, batch)
                    if unresolved:
                        # Renamed companies we could not match by name: binary-search which
                        # company owns each old name, among those without rows for its periods.
                        periods = {(r.year, r.period) for r in rows if r.company_name in unresolved}
                        suspects = [c for c in batch
                                    if not periods <= {(r.year, r.period) for r in assigned.get(c.oid, [])}]
                        log.info("Resolving renamed companies %s among %d candidates",
                                 sorted(unresolved), len(suspects))
                        owners = self.locate_names(unresolved, suspects, sector, year_pair, item_chunk, labels)
                        for oid, names in owners.items():
                            assigned.setdefault(oid, []).extend(r for r in rows if r.company_name in names)
                            by_oid[oid].aliases.update(normalize_name(n) for n in names)
                            self.learn_aliases(oid, names)
                        if unresolved - {n for names in owners.values() for n in names}:
                            log.warning("Could not attribute rows for %s", sorted(unresolved))
                    for oid, company_rows in assigned.items():
                        for r in company_rows:
                            key = (oid, r.year, r.period, r.nature, r.notification_id)
                            if key in merged:
                                merged[key][1].values.update(r.values)
                            else:
                                merged[key] = (by_oid[oid], r)
                results.extend(merged.values())


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def to_table_row(company: Company, row: ExportRow) -> dict:
    spec = FIELD_MAP[company.sector]
    end = period_end(company, row.year, row.period)
    if row.publish_date and row.publish_date < end:
        log.warning("%s %d Q%d published %s before computed period end %s - non-calendar fiscal year? "
                    "Add it to FISCAL_YEAR_START_MONTH.", company.symbols[0], row.year, row.period,
                    row.publish_date, end)
    out = {col: None for col in TABLE_COLUMNS}
    out.update(
        CompanyId=company.company_id,
        FiscalYear=row.year,
        FiscalQuarter=row.period,
        PeriodEndDate=end.isoformat(),
        PublishDate=row.publish_date.isoformat() if row.publish_date else None,
    )
    for col, fn in spec.fields.items():
        out[col] = fn(row.values)
    out.update(
        PresentationCurrency=row.currency, StockCode=company.stock_code, CompanyName=row.company_name,
        StatementNature=row.nature, SectoralStatementType=row.sectoral_type,
        NotificationId=row.notification_id,
    )
    return out


def pick_preferred(rows: list[tuple[Company, ExportRow]]) -> dict[tuple, tuple[Company, ExportRow]]:
    """Primary key is (CompanyId, FiscalYear, FiscalQuarter): prefer consolidated
    statements, then the most recently published one."""
    best: dict[tuple, tuple[Company, ExportRow]] = {}
    for company, row in rows:
        key = (company.company_id, row.year, row.period)
        rank = (row.nature.lower().startswith("consolidated"), row.publish_date or dt.date.min,
                int(row.notification_id or 0))
        cur = best.get(key)
        if cur is None or rank > (cur[1].nature.lower().startswith("consolidated"),
                                  cur[1].publish_date or dt.date.min, int(cur[1].notification_id or 0)):
            best[key] = (company, row)
    return best


def fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:f}"
    return str(value)


def write_csv(path: Path, columns: list[str], rows: Iterable[dict], encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.part")
    with tmp.open("w", newline="", encoding=encoding) as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: fmt(r.get(k)) for k in columns})
    tmp.replace(path)


def read_existing(path: Path) -> dict[tuple, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {(int(r["CompanyId"]), int(r["FiscalYear"]), int(r["FiscalQuarter"])): r for r in csv.DictReader(f)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def fiscal_years(args: argparse.Namespace, today: dt.date) -> list[int]:
    """--update: previous + current year. Otherwise the years of --start..--end, where
    --start defaults to 1 January --years years ago and --end to today."""
    if args.update:
        return [today.year - 1, today.year]
    first = args.start.year if args.start else today.year - args.years
    last = args.end.year if args.end else today.year
    if first < FIRST_YEAR:
        log.warning("KAP's item search starts in %d; fetching from %d", FIRST_YEAR, FIRST_YEAR)
        first = FIRST_YEAR
    return list(range(first, last + 1))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m stock_crawler fundamentals",
                                description="Crawl KAP financial statement items into a CompanyFundamental CSV.")
    p.add_argument("--years", type=int, default=5,
                   help=f"fiscal years back from the current one (default: %(default)s; KAP starts in {FIRST_YEAR})")
    p.add_argument("--start", type=dt.date.fromisoformat, help="first fiscal year, as YYYY-MM-DD (overrides --years)")
    p.add_argument("--end", type=dt.date.fromisoformat, help="last fiscal year, as YYYY-MM-DD (default: this year)")
    p.add_argument("--update", action="store_true",
                   help="incremental: previous + current year only, merged into the existing CSV")
    p.add_argument("--symbols", help="comma separated stock codes to restrict to (e.g. ASELS,THYAO)")
    p.add_argument("--sectors", default=",".join(FIELD_MAP), help="statement types to crawl (default: all)")
    p.add_argument("--base-url", default=BASE_URL, help="KAP website (default: %(default)s)")
    p.add_argument("--out-dir", type=Path, default=Path("output/fundamentals"),
                   help="output directory (default: %(default)s)")
    p.add_argument("--cache-dir", type=Path, default=Path("cache/fundamentals"), help="(default: %(default)s)")
    p.add_argument("--refresh", action="store_true", help="ignore the cache and re-download everything in scope")
    p.add_argument("--recent-ttl-hours", type=float, default=12, help="cache lifetime for recent years")
    p.add_argument("--old-ttl-days", type=float, default=30, help="cache lifetime for years older than last year")
    p.add_argument("--min-delay", type=float, default=30.0, help="min seconds between requests")
    p.add_argument("--max-delay", type=float, default=60.0, help="max seconds between requests")
    p.add_argument("--long-pause-every", type=int, default=40, help="take a longer break every N requests (0=off)")
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    p.add_argument("--with-meta", action="store_true",
                   help="append StockCode, CompanyName, ... columns to the main CSV")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.years < 1:
        p.error("--years must be at least 1")
    if args.start and args.end and args.start > args.end:
        p.error("--start must not be after --end")
    args.base_url = args.base_url.rstrip("/")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    years = fiscal_years(args, dt.date.today())
    if not years:
        log.error("No fiscal years to fetch (KAP's item search starts in %d)", FIRST_YEAR)
        return 2
    sectors = [s.strip().upper() for s in args.sectors.split(",") if s.strip()]
    unknown = set(sectors) - set(FIELD_MAP)
    if unknown:
        log.error("Unknown sector(s): %s (valid: %s)", ", ".join(unknown), ", ".join(FIELD_MAP))
        return 2
    wanted = {s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None

    cache = Cache(args.cache_dir, args.recent_ttl_hours, args.old_ttl_days, args.refresh)
    http = PoliteClient(
        base_url=args.base_url, min_delay=args.min_delay, max_delay=args.max_delay,
        long_pause_every=args.long_pause_every, long_pause=(30.0, 90.0), max_retries=args.max_retries,
        user_agent=args.user_agent,
    )
    crawler = KapCrawler(http, cache)
    started = time.monotonic()
    fetched: list[tuple[Company, ExportRow]] = []
    exit_code = 0
    try:
        with http:
            listed = crawler.listed_companies()
            log.info("%d listed companies on KAP", len(listed))
            by_sector: list[tuple[str, list[Company]]] = []
            for sector in sectors:
                members = [listed[m["mkkMemberOid"]] for m in crawler.sector_members(sector)
                           if m.get("mkkMemberOid") in listed]
                for c in members:
                    c.sector = c.sector or sector
                if wanted:
                    members = [c for c in members if wanted & set(c.symbols)]
                members = [c for c in members if c.sector == sector]
                if not members:
                    continue
                members.sort(key=lambda c: c.symbols[0])
                by_sector.append((sector, members))
            # Newest years first, for every sector, so a stopped run already has the recent data.
            for year_pair in newest_first(years):
                for sector, members in by_sector:
                    log.info("%s: %d companies x years %s", sector, len(members), "-".join(map(str, year_pair)))
                    crawler.fetch_sector(sector, members, year_pair, fetched)
            no_sector = [c.stock_code for c in listed.values() if not c.sector
                         and (not wanted or wanted & set(c.symbols))]
            if no_sector and set(sectors) == set(FIELD_MAP):
                log.info("No item-search data offered for: %s", ", ".join(sorted(no_sector)))
    except (BlockedError, httpx.HTTPError) as exc:
        log.error("%s", exc)
        exit_code = 3
    except KeyboardInterrupt:
        log.warning("Interrupted - writing what was fetched so far (re-run to resume from cache)")
        exit_code = 130

    # ---- write outputs ----
    best = pick_preferred(fetched)
    new_rows = {k: to_table_row(c, r) for k, (c, r) in best.items()}
    columns = TABLE_COLUMNS + [UNIT_COLUMN] + (META_COLUMNS if args.with_meta else [])
    main_csv = args.out_dir / "company_fundamental.csv"
    merge = bool(args.update or wanted or exit_code)
    merged = read_existing(main_csv) if merge else {}
    if any(UNIT_COLUMN not in r for r in merged.values()):
        log.warning("%s was written by an older version (values scaled to whole TRY), so it is replaced, "
                    "not merged. Run without --update to rebuild every year.", main_csv)
        merged, merge = {}, False
    merged.update(new_rows)
    write_csv(main_csv, columns, (merged[k] for k in sorted(merged)))

    raw_csv = args.out_dir / "company_fundamental_raw.csv"
    refreshed = {(c.company_id, r.year, r.period) for c, r in fetched}
    raw = []
    if raw_csv.exists() and merge:
        with raw_csv.open(newline="", encoding="utf-8-sig") as f:
            raw = [r for r in csv.DictReader(f)
                   if (int(r["CompanyId"]), int(r["FiscalYear"]), int(r["FiscalQuarter"])) not in refreshed]
    for company, row in fetched:
        for item, value in sorted(row.values.items()):
            raw.append({
                "CompanyId": company.company_id, "StockCode": company.stock_code, "CompanyName": row.company_name,
                "FiscalYear": row.year, "FiscalQuarter": row.period, "PublishDate": row.publish_date,
                "NotificationId": row.notification_id, "StatementNature": row.nature,
                "SectoralStatementType": row.sectoral_type, "PresentationCurrency": row.currency,
                "Multiplier": row.multiplier, "ItemId": item,
                "Value": value,
            })
    raw_cols = ["CompanyId", "StockCode", "CompanyName", "FiscalYear", "FiscalQuarter", "PublishDate",
                "NotificationId", "StatementNature", "SectoralStatementType", "PresentationCurrency",
                "Multiplier", "ItemId", "Value"]
    raw.sort(key=lambda r: (int(r["CompanyId"]), int(r["FiscalYear"]), int(r["FiscalQuarter"]), r["ItemId"]))
    write_csv(raw_csv, raw_cols, raw, encoding="utf-8-sig")

    seen = {c.oid: c for c, _ in fetched}
    write_csv(args.out_dir / "companies.csv", ["CompanyId", "StockCode", "CompanyName", "KapMemberOid", "Sector"],
              ({"CompanyId": c.company_id, "StockCode": c.stock_code, "CompanyName": c.title,
                "KapMemberOid": c.oid, "Sector": c.sector} for c in sorted(seen.values(), key=lambda c: c.company_id)),
              encoding="utf-8-sig")

    log.info("Done in %.1f min: %d HTTP requests, %d new/updated rows, %d rows in %s",
             (time.monotonic() - started) / 60, http.requests_made, len(new_rows), len(merged), main_csv)
    return exit_code
