"""Export daily Borsa İstanbul stock prices (OHLCV) for the last N years to CSV.

Sources (both official and free):
  - Borsa İstanbul Equity Market daily bulletin,
    https://www.borsaistanbul.com/data/thb/YYYY/MM/thbYYYYMMDD1.zip. One file per trading day
    covers every listed stock, so five years is ~1,300 downloads however many stocks you want.
  - Central Bank of Türkiye (TCMB) daily exchange rates, https://www.tcmb.gov.tr/kurlar/,
    for the USD/TRY rate and USD prices.
Downloaded days are cached, so later runs only fetch the new days.

Run: python -m stock_crawler market_data
Python 3.10+ standard library only; uses the shared HTTP client in stock_crawler/http_client.py.
See README.md for column definitions.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import io
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Callable
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from ..dates import years_before
from ..http_client import FetchError, HttpClient, NotFound

OUTPUT_CSV = Path("output/market_data/market_data.csv")
COMPANIES_CSV = Path("output/companies/companies.csv")  # written by the companies crawler

# Source URL templates (--bulletin-url, --fx-url) and the cache file for each day.
BULLETIN_URL = "https://www.borsaistanbul.com/data/thb/{d:%Y}/{d:%m}/thb{d:%Y%m%d}1.zip"
BULLETIN_FILE = "{d:%Y}/thb{d:%Y%m%d}1.zip"
FX_URL = "https://www.tcmb.gov.tr/kurlar/{d:%Y%m}/{d:%d%m%Y}.xml"
FX_FILE = "tcmb/{d:%Y}/{d:%Y%m%d}.xml"
# English bulletin header -> output column. Columns are looked up by name because the layout
# changes over the years (e.g. 2016 bulletins have an extra MIDDAY PRICE column).
COLUMNS = {
    "OPENING PRICE": "Open",
    "HIGHEST PRICE": "High",
    "LOWEST PRICE": "Low",
    "CLOSING PRICE": "Close",
    "VWAP": "Vwap",
    "TOTAL TRADED VOLUME": "Volume",
    "TOTAL TRADED VALUE": "TradedValue",
    "TOTAL NUMBER OF CONTRACTS": "Trades",
    "PREVIOUS LAST PRICE": "PreviousClose",
    "CHANGE TO PREVIOUS CLOSING (%)": "ChangePercent",
    "CORPORATE ACTION": "CorporateAction",
}
CSV_FIELDS = ["Ticker", "Date", *COLUMNS.values()]
USD_FIELDS = ["UsdTry", "CloseUsd"]
# --db-output: the columns of the MarketData table, and each value's source column and SQL type
# as (source index in a row, integer digits, decimal places). decimal(18,4) = 14 + 4 digits.
DB_FIELDS = ["TradeDate", "CompanyId", "OpenPrice", "HighPrice", "LowPrice", "ClosePrice",
             "Volume", "ValueTraded", "SourcePriority"]
DB_VALUES = {"OpenPrice": (2, 14, 4), "HighPrice": (3, 14, 4), "LowPrice": (4, 14, 4),
             "ClosePrice": (5, 14, 4), "Volume": (7, 18, 0), "ValueTraded": (8, 20, 4)}
# A weekday this old without a bulletin is a holiday, not a bulletin that is not uploaded yet.
SETTLED_DAYS = 7
LOG = logging.getLogger("market_data")


# ----------------------------------------------------------------------- bulletins

def is_bulletin(body: bytes) -> bool:
    """A complete zip with one CSV inside (truncated downloads have no central directory)."""
    try:
        with ZipFile(io.BytesIO(body)) as book:
            return any(name.lower().endswith(".csv") for name in book.namelist())
    except BadZipFile:
        return False


def read_bulletin(data: bytes) -> list[list[str]]:
    """Rows [ticker, date, *COLUMNS] for every ordinary share (code `XXXXX.E`) traded that day.
    Values are copied as the bulletin writes them (older bulletins write `.376` for 0.376)."""
    with ZipFile(io.BytesIO(data)) as book:
        names = [n for n in book.namelist() if n.lower().endswith(".csv")]
        raw = book.read(names[0])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1254")

    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("TRADE DATE;"))
    except StopIteration:
        raise ValueError("bulletin has no English header row") from None
    header = {name.strip(): i for i, name in enumerate(lines[start].split(";"))}
    needed = {"TRADE DATE", "INSTRUMENT SERIES CODE", "INSTRUMENT GROUP", *COLUMNS}
    if missing := needed - header.keys():
        raise ValueError(f"bulletin lacks columns {sorted(missing)}")

    rows, seen = [], set()
    # Only ~7% of lines are shares; skip the rest before the (slower) CSV parsing.
    for row in csv.reader((line for line in lines[start + 1:] if ".E;" in line), delimiter=";"):
        code = row[header["INSTRUMENT SERIES CODE"]].strip()
        if row[header["INSTRUMENT GROUP"]].strip() != "EQT" or not code.endswith(".E"):
            continue
        try:
            traded = float(row[header["TOTAL TRADED VOLUME"]] or 0) > 0
        except ValueError:
            traded = False
        ticker = code[:-2]
        if not traded or ticker in seen:  # untraded days have zero prices
            continue
        seen.add(ticker)
        rows.append([ticker, iso_date(row[header["TRADE DATE"]]), *(row[header[name]] for name in COLUMNS)])
    return rows


def iso_date(text: str) -> str:
    """TRADE DATE as YYYY-MM-DD. Twelve bulletins of May-June 2020 write it as 21.05.2020."""
    if "." not in text:
        return text
    day, month, year = text.split(".")
    return date(int(year), int(month), int(day)).isoformat()


def is_fx_file(body: bytes) -> bool:
    return b"Tarih_Date" in body and b'Kod="USD"' in body and body.rstrip().endswith(b"</Tarih_Date>")


def read_usd_try(data: bytes) -> str:
    """TCMB's USD forex buying rate, the indicative rate it publishes at 15:30 each business day."""
    rate = ET.fromstring(data).findtext("Currency[@Kod='USD']/ForexBuying", "").strip()
    if not rate:
        raise ValueError("TCMB file has no USD rate")
    return rate


class DailyStore:
    """Downloads one file per day once and keeps it; published days never change."""

    def __init__(self, http: HttpClient, directory: Path, url: str, file: str,
                 validate: Callable[[bytes], bool]) -> None:
        self.http = http
        self.directory = directory
        self.url = url
        self.file = file
        self.validate = validate

    def get(self, day: date) -> bytes | None:
        """The day's file, or None if none was published (holiday, or not uploaded yet)."""
        path = self.directory / self.file.format(d=day)
        holiday = path.with_suffix(".none")
        if holiday.exists():
            return None
        if path.exists():
            data = path.read_bytes()
            if self.validate(data):
                return data
        try:
            data = self.http.get(self.url.format(d=day), validate=self.validate)
        except NotFound:
            if (date.today() - day).days >= SETTLED_DAYS:
                holiday.parent.mkdir(parents=True, exist_ok=True)
                holiday.touch()
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return data


def collect(store: DailyStore, fx_store: DailyStore | None, days: list[date], tickers: set[str] | None,
            cooldown: float, max_cooldowns: int) -> tuple[dict[str, list[str]], list[date], list[date]]:
    """CSV lines per ticker (in date order), trading days found, and days that failed.

    With `fx_store`, each row also gets TCMB's USD/TRY rate. TCMB publishes no rate on its own
    holidays; its rule is that the last published rate stays valid until the next one, so that
    rate is used.

    When a day still fails after the client's retries, the server is probably refusing us for
    a while: pause `cooldown` seconds and retry that day. Stop after `max_cooldowns` pauses in
    a row did not help.
    """
    lines: dict[str, list[str]] = {}
    found: list[date] = []
    usd_try = ""
    cooldowns = 0
    started = time.monotonic()
    index = 0
    while index < len(days):
        day = days[index]
        try:
            data = store.get(day)
            rows = read_bulletin(data) if data else []
            fx = fx_store.get(day) if fx_store else None
            rate = read_usd_try(fx) if fx else usd_try
        except (FetchError, ValueError, BadZipFile, ET.ParseError) as exc:
            if cooldowns >= max_cooldowns:
                LOG.error("Still failing after %s cool-downs (%s); stopping.", cooldowns, exc)
                return lines, found, days[index:]
            cooldowns += 1
            LOG.warning("%s: %s. Cooling down for %.0f min (%s/%s) before retrying.",
                        day, exc, cooldown / 60, cooldowns, max_cooldowns)
            store.http.close()  # start again with fresh connections
            if fx_store:
                fx_store.http.close()
            time.sleep(cooldown)
            continue
        cooldowns = 0
        index += 1
        usd_try = rate
        if data:
            found.append(day)
        for row in rows:
            if tickers is None or row[0] in tickers:
                if fx_store:
                    row += [usd_try, _usd(row[5], usd_try)]
                lines.setdefault(row[0], []).append(_csv_line(row))
        if index % 50 == 0 or index == len(days):
            LOG.info("Days %s/%s up to %s (%.0f min elapsed, request gap %.1fs)", index, len(days),
                     day, (time.monotonic() - started) / 60, store.http.throttle.interval)
    return lines, found, []


# ------------------------------------------------------------------------ output

def _usd(price: str, usd_try: str) -> str:
    if not usd_try:
        return ""
    return f"{float(price) / float(usd_try):.6f}".rstrip("0").rstrip(".")


def _csv_line(values: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(values)
    return buffer.getvalue()


def write_atomic(path: Path, header: list[str], tickers: list[str], lines: dict[str, list[str]]) -> None:
    """The old file is only replaced once the new one is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(_csv_line(header))
            for ticker in tickers:
                out.writelines(lines[ticker])
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            temp.unlink(missing_ok=True)
            raise
    temp.replace(path)


def _sql_number(value: str, integer_digits: int, places: int) -> str:
    """`value` unchanged if it fits SQL decimal(integer_digits + places, places) (bigint: places 0)."""
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}") from None
    if not number.is_finite() or number != round(number, places) or abs(number) >= 10 ** integer_digits:
        raise ValueError(f"{value!r} does not fit {integer_digits + places} digits with {places} decimals")
    return value


def db_rows(lines: dict[str, list[str]], company_ids: dict[str, str],
            priority: int) -> dict[str, list[str]]:
    """CSV lines in the MarketData table's column order, per ticker that has a CompanyId."""
    db: dict[str, list[str]] = {}
    for ticker in lines.keys() & company_ids.keys():
        converted = []
        for row in csv.reader(lines[ticker]):
            try:
                values = {name: _sql_number(row[index], digits, places)
                          for name, (index, digits, places) in DB_VALUES.items()}
            except ValueError as exc:
                raise ValueError(f"{ticker} {row[1]}: {exc}") from None
            converted.append(_csv_line([row[1], company_ids[ticker], values["OpenPrice"], values["HighPrice"],
                                        values["LowPrice"], values["ClosePrice"], values["Volume"],
                                        values["ValueTraded"], str(priority)]))
        db[ticker] = converted
    return db


def read_company_ids(path: Path) -> dict[str, str]:
    """Ticker -> CompanyId from a CSV with those two columns (exported from the Company table)."""
    ids: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        for line, row in enumerate(csv.DictReader(source), start=2):
            ticker, company_id = (row.get("Ticker") or "").strip(), (row.get("CompanyId") or "").strip()
            if not ticker or not company_id.isdigit():
                raise ValueError(f"{path} line {line}: need a Ticker and a numeric CompanyId")
            if ids.get(ticker, company_id) != company_id:
                raise ValueError(f"{path}: ticker {ticker} has two CompanyIds")
            ids[ticker] = company_id
    if len(set(ids.values())) != len(ids):
        raise ValueError(f"{path}: a CompanyId is used by more than one ticker "
                         "(that would break the (TradeDate, CompanyId) primary key)")
    if not ids:
        raise ValueError(f"No rows in {path}")
    return ids


def read_tickers(path: Path) -> set[str]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        tickers = {row["Ticker"].strip() for row in csv.DictReader(source) if row.get("Ticker", "").strip()}
    if not tickers:
        raise ValueError(f"No tickers in {path} (expected a 'Ticker' column)")
    return tickers


# -------------------------------------------------------------------------- main

def run(args: argparse.Namespace) -> int:
    tickers = None if args.all_equities else read_tickers(args.companies)
    company_ids = read_company_ids(args.company_ids) if args.db_output else {}  # fail before fetching
    end = args.end or date.today()
    start = args.start or years_before(end, args.years)
    days = [start + timedelta(n) for n in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    LOG.info("%s to %s: %s weekdays, %s", start, end, len(days),
             "all equities" if tickers is None else f"{len(tickers)} tickers from {args.companies}")

    # One client per host, so each server gets its own throttle and connection.
    clients = [HttpClient(args.fetch_backend, args.proxy, args.request_interval,
                          max_interval=args.max_request_interval, ca_bundle=args.ca_bundle)
               for _ in range(1 if args.no_usd else 2)]
    store = DailyStore(clients[0], args.cache_dir, args.bulletin_url, BULLETIN_FILE, is_bulletin)
    fx_store = None if args.no_usd else DailyStore(clients[1], args.cache_dir, args.fx_url, FX_FILE, is_fx_file)
    try:
        lines, found, failed = collect(store, fx_store, days, tickers,
                                       args.cooldown_minutes * 60, args.max_cooldowns)
    finally:
        for client in clients:
            client.close()

    if not found:
        LOG.error("No bulletins found between %s and %s; %s not written.", start, end, args.output)
        return 1
    if (found[0] - start).days > SETTLED_DAYS:
        LOG.warning("First bulletin is %s, later than the requested start %s", found[0], start)
    rows = sum(len(v) for v in lines.values())
    LOG.info("%s trading days (%s to %s), %s stocks, %s rows", len(found), found[0], found[-1],
             len(lines), rows)
    if tickers is not None and (absent := sorted(tickers - lines.keys())):
        LOG.warning("%s tickers have no trades in this period (e.g. non-equity codes, suspended, "
                    "renamed): %s%s", len(absent), ", ".join(absent[:15]), ", ..." if len(absent) > 15 else "")
    if failed and not args.allow_incomplete:
        LOG.error("%s days could not be fetched (from %s). %s was NOT written. Rerun later; "
                  "downloaded days are cached. Use --allow-incomplete to write anyway.",
                  len(failed), failed[0], args.output)
        return 2
    if failed:
        LOG.warning("Writing incomplete output: %s days missing from %s", len(failed), failed[0])

    header = CSV_FIELDS if args.no_usd else CSV_FIELDS + USD_FIELDS
    ordered = sorted(lines)
    write_atomic(args.output, header, ordered, lines)
    LOG.info("Saved %s rows to %s", rows, args.output)
    if args.split_dir:
        for ticker in ordered:
            write_atomic(args.split_dir / f"{ticker}.csv", header, [ticker], lines)
        LOG.info("Saved %s per-ticker files to %s", len(ordered), args.split_dir)
    if args.db_output:
        db = db_rows(lines, company_ids, args.source_priority)
        if unmapped := sorted(lines.keys() - db.keys()):
            LOG.warning("%s tickers have prices but no CompanyId in %s and are left out of %s: %s%s",
                        len(unmapped), args.company_ids, args.db_output, ", ".join(unmapped[:15]),
                        ", ..." if len(unmapped) > 15 else "")
        write_atomic(args.db_output, DB_FIELDS, sorted(db), db)
        LOG.info("Saved %s MarketData rows for %s companies to %s",
                 sum(len(v) for v in db.values()), len(db), args.db_output)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m stock_crawler market_data", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, default=OUTPUT_CSV, help="(default: %(default)s)")
    ap.add_argument("--split-dir", type=Path, help="Also write one CSV per ticker into this directory")
    ap.add_argument("--db-output", type=Path,
                    help="Also write a CSV in the MarketData table's columns (needs --company-ids)")
    ap.add_argument("--company-ids", type=Path,
                    help="CSV with Ticker,CompanyId columns, exported from your Company table")
    ap.add_argument("--source-priority", type=int, default=1,
                    help="SourcePriority written to every --db-output row (default: 1, official bulletin)")
    ap.add_argument("--companies", type=Path, default=COMPANIES_CSV,
                    help="CSV with a Ticker column, from the companies crawler (default: %(default)s)")
    ap.add_argument("--all-equities", action="store_true",
                    help="Every share in the bulletins, including delisted ones; ignores --companies")
    ap.add_argument("--years", type=int, default=5, help="How many years back from --end (default: 5)")
    ap.add_argument("--start", type=date.fromisoformat, help="First day, YYYY-MM-DD (overrides --years)")
    ap.add_argument("--end", type=date.fromisoformat, help="Last day, YYYY-MM-DD (default: today)")
    ap.add_argument("--no-usd", action="store_true", help="Skip the TCMB USD/TRY rate and USD prices")
    ap.add_argument("--bulletin-url", default=BULLETIN_URL,
                    help="Borsa İstanbul daily bulletin URL; {d:...} is the day (default: %(default)s)")
    ap.add_argument("--fx-url", default=FX_URL,
                    help="TCMB daily exchange rate URL; {d:...} is the day (default: %(default)s)")
    ap.add_argument("--cache-dir", type=Path, default=Path("cache/market_data"), help="(default: %(default)s)")
    ap.add_argument("--proxy", help="HTTP or SOCKS5 proxy for all requests, e.g. http://127.0.0.1:12334")
    ap.add_argument("--fetch-backend", choices=("auto", "python", "curl"), default="auto",
                    help="HTTP client; auto switches to curl if Python's client cannot connect")
    ap.add_argument("--ca-bundle", help="PEM file of trusted CAs for the Python client (e.g. a corporate/VPN root)")
    ap.add_argument("--request-interval", type=float, default=1.0,
                    help="Normal seconds between requests, +/-30%% jitter (default: 1)")
    ap.add_argument("--max-request-interval", type=float, default=60.0,
                    help="Upper limit when slowing down after push-back from the server (default: 60)")
    ap.add_argument("--cooldown-minutes", type=float, default=5.0,
                    help="Pause when a server keeps refusing requests, then retry (default: 5)")
    ap.add_argument("--max-cooldowns", type=int, default=3,
                    help="Stop after this many cool-downs in a row without success (default: 3)")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="Write the CSV even if some days could not be fetched")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.proxy and args.fetch_backend == "python":
        ap.error("--proxy uses curl; choose --fetch-backend auto or curl")
    if args.proxy and not args.proxy.startswith(("http://", "https://", "socks5://", "socks5h://")):
        ap.error("--proxy must be an http://, https://, socks5:// or socks5h:// URL")
    if not 1 <= args.years <= 40:
        ap.error("--years must be between 1 and 40")
    if args.db_output and not args.company_ids:
        ap.error("--db-output needs --company-ids (Ticker,CompanyId from your Company table)")
    if not 0 <= args.source_priority <= 255:
        ap.error("--source-priority must fit tinyint (0-255)")
    if args.start and args.end and args.start > args.end:
        ap.error("--start must not be after --end")
    if not 0.5 <= args.request_interval <= 120:
        ap.error("--request-interval must be between 0.5 and 120 seconds")
    if args.max_request_interval < args.request_interval:
        ap.error("--max-request-interval must be >= --request-interval")
    if args.cooldown_minutes < 0 or args.max_cooldowns < 0:
        ap.error("--cooldown-minutes and --max-cooldowns must not be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        LOG.error("Interrupted; %s not written. Downloaded days are kept for the next run.", args.output)
        return 130
    except (RuntimeError, ValueError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
