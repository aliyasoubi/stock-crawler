"""Export Borsa İstanbul listed stocks (equities) to CSV from official online sources.

Sources:
  - KAP BIST companies directory: ticker, legal name, profile link
  - Borsa İstanbul ilkislem.zip: which codes are equities, listing date
  - KAP company profile / financial summary pages: sector, reporting currency

Run: python -m stock_crawler companies
Python 3.10+ standard library only. See README.md for field definitions.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, replace
from html.parser import HTMLParser
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urljoin

from ..http_client import FetchError, HttpClient, NotFound
from .parsers import Listing, parse_financial_currency, parse_profile_sector, read_listing_workbook

DIRECTORY_URL = "https://kap.org.tr/tr/bist-sirketler"
LISTING_URL = "https://www.borsaistanbul.com/datum/ilkislem.zip"
OUTPUT_CSV = Path("output/companies/companies.csv")
# Paths in company links, relative to the KAP site of DIRECTORY_URL.
PROFILE_PREFIX = "/tr/sirket-bilgileri/ozet/"
FINANCIAL_PREFIX = "/tr/sirket-finansal-bilgileri/"
CODE = re.compile(r"^[A-Z0-9]{2,32}$")
CSV_FIELDS = ["Ticker", "MarketId", "FullName", "SectorName", "ReportingCurrency",
              "IsActive", "IpoDate", "IpoDateSource"]
LOG = logging.getLogger("turkey_companies")


@dataclass(frozen=True)
class Company:
    ticker: str
    full_name: str
    profile_path: str = ""
    sector_name: str | None = None
    reporting_currency: str | None = None
    listing: Listing | None = None


def is_kap_page(body: bytes) -> bool:
    """KAP sometimes answers HTTP 200 with an empty or truncated body while throttling."""
    return len(body) > 20_000 and b"</html>" in body[-4096:] and b"sirket-bilgileri" in body


# ---------------------------------------------------------------------- directory

class DirectoryParser(HTMLParser):
    """Read rows of KAP's company directory table (id="financialTable")."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.in_row = self.in_cell = False
        self.cells: list[str] = []
        self.parts: list[str] = []
        self.row_link = ""
        self.rows: list[tuple[list[str], str]] = []

    def handle_starttag(self, tag, attrs):
        attrs_map = dict(attrs)
        if tag == "table" and (self.depth or attrs_map.get("id") == "financialTable"):
            self.depth += 1
        elif self.depth and tag == "tr":
            self.in_row, self.cells, self.row_link = True, [], ""
        elif self.in_row and tag == "td":
            self.in_cell, self.parts = True, []
        elif self.in_cell and tag == "a" and not self.cells:
            href = attrs_map.get("href") or ""
            if href.startswith(PROFILE_PREFIX):
                self.row_link = href

    def handle_data(self, data):
        if self.in_cell:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.in_cell:
            self.cells.append(" ".join("".join(self.parts).split()))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if len(self.cells) >= 2:
                self.rows.append((self.cells, self.row_link))
            self.in_row = False
        elif tag == "table" and self.depth:
            self.depth -= 1


def parse_directory(html: str, minimum: int = 100) -> list[Company]:
    parser = DirectoryParser()
    parser.feed(html)
    found: dict[str, Company] = {}
    for (codes, full_name, *_), link in parser.rows:
        # One company can have several codes, e.g. "A1CAP ACP" (share + debt issuer code).
        for ticker in re.split(r"[,\s]+", codes):
            if not CODE.fullmatch(ticker) or not full_name:
                continue
            existing = found.get(ticker)
            if existing and existing.full_name != full_name:
                raise ValueError(f"Ticker {ticker} appears under two companies")
            found[ticker] = Company(ticker, full_name, link)
    if len(found) < minimum:
        raise ValueError(f"Only {len(found)} codes parsed from KAP; page layout may have changed")
    return sorted(found.values(), key=lambda c: c.ticker)


# ------------------------------------------------------------------- KAP details

class PageCache:
    """Validated on-disk cache so an interrupted run resumes without refetching."""

    def __init__(self, http: HttpClient, directory: Path, max_age_days: float) -> None:
        self.http = http
        self.directory = directory
        self.max_age = max_age_days * 86400
        directory.mkdir(parents=True, exist_ok=True)

    def get(self, url: str, name: str) -> str | None:
        """Page text, or None if KAP has no such page (HTTP 404)."""
        path = self.directory / name
        try:
            if time.time() - path.stat().st_mtime < self.max_age:
                body = path.read_bytes()
                if is_kap_page(body):
                    return body.decode("utf-8")
        except FileNotFoundError:
            pass
        try:
            body = self.http.get(url, validate=is_kap_page)
        except NotFound:
            LOG.info("No KAP page at %s", url)
            return None
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(body)
        temporary.replace(path)
        return body.decode("utf-8")


def fetch_details(cache: PageCache, site: str, profile_path: str) -> tuple[str | None, str | None]:
    """Sector and reporting currency for one KAP company; `site` is any URL on the KAP site."""
    slug = profile_path.removeprefix(PROFILE_PREFIX)
    identifier = slug.split("-")[0]
    if not identifier.isdecimal():
        return None, None
    profile = cache.get(urljoin(site, profile_path), f"{identifier}_profile.html")
    financial = cache.get(urljoin(site, FINANCIAL_PREFIX + slug), f"{identifier}_financial.html")
    return (parse_profile_sector(profile) if profile else None,
            parse_financial_currency(financial) if financial else None)


def add_details(companies: list[Company], cache: PageCache, site: str, cooldown: float,
                max_cooldowns: int) -> tuple[list[Company], list[str]]:
    """Return enriched companies and the tickers whose KAP pages could not be fetched.

    When a company still fails after the client's retries, KAP is usually blocking us for a
    while: pause `cooldown` seconds and retry the same company. Stop only after
    `max_cooldowns` pauses in a row did not help.
    """
    paths = sorted({c.profile_path for c in companies if c.profile_path})
    details: dict[str, tuple[str | None, str | None]] = {}
    failed: set[str] = set()
    cooldowns = 0
    started = time.monotonic()
    index = 0
    while index < len(paths):
        path = paths[index]
        try:
            details[path] = fetch_details(cache, site, path)
        except (FetchError, UnicodeDecodeError) as exc:
            if cooldowns >= max_cooldowns:
                LOG.error("Still failing after %s cool-downs (%s); stopping.", cooldowns, exc)
                failed.update(paths[index:])
                break
            cooldowns += 1
            LOG.warning("%s. Cooling down for %.0f min (%s/%s) before retrying.",
                        exc, cooldown / 60, cooldowns, max_cooldowns)
            cache.http.close()  # start again with a fresh connection
            time.sleep(cooldown)
            continue
        cooldowns = 0
        index += 1
        if index % 25 == 0 or index == len(paths):
            LOG.info("KAP details %s/%s (%.0f min elapsed, request gap %.1fs)", index, len(paths),
                     (time.monotonic() - started) / 60, cache.http.throttle.interval)
    result = [replace(c, sector_name=details.get(c.profile_path, (None, None))[0],
                      reporting_currency=details.get(c.profile_path, (None, None))[1])
              for c in companies]
    return result, sorted(c.ticker for c in companies if c.profile_path in failed)


# ------------------------------------------------------------------------ output

def ipo_date(company: Company) -> tuple[str, str]:
    """IpoDate = Borsa listing date; pre-1990s listings only have the first trading day."""
    listing = company.listing
    if listing and listing.listing_date:
        return listing.listing_date.isoformat(), "listing_date"
    if listing and listing.first_trading_date:
        return listing.first_trading_date.isoformat(), "first_trading_day"
    return "", ""


def write_csv(companies: list[Company], path: Path, market_id: int | None) -> None:
    """Write atomically: the old file is only replaced once the new one is complete."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as out:
        temp = Path(out.name)
        try:
            writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for c in companies:
                date_value, date_source = ipo_date(c)
                writer.writerow({"Ticker": c.ticker, "MarketId": "" if market_id is None else market_id,
                                 "FullName": c.full_name, "SectorName": c.sector_name or "",
                                 "ReportingCurrency": c.reporting_currency or "", "IsActive": 1,
                                 "IpoDate": date_value, "IpoDateSource": date_source})
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            out.close()
            temp.unlink(missing_ok=True)
            raise
    temp.replace(path)


# -------------------------------------------------------------------------- main

def run(args: argparse.Namespace) -> int:
    http = HttpClient(args.fetch_backend, args.proxy, args.request_interval,
                      max_interval=args.max_request_interval, ca_bundle=args.ca_bundle)
    try:
        return _run(args, http)
    finally:
        http.close()


def _run(args: argparse.Namespace, http: HttpClient) -> int:

    LOG.info("Fetching KAP directory %s", args.directory_url)
    directory = parse_directory(http.get(args.directory_url, validate=lambda b: b"financialTable" in b)
                                .decode("utf-8"), minimum=args.min_companies)
    LOG.info("Fetching Borsa İstanbul listing workbook %s", args.listing_url)
    listings = read_listing_workbook(http.get(args.listing_url, validate=lambda b: b[:2] == b"PK"))

    companies = [replace(c, listing=listings[c.ticker]) for c in directory if c.ticker in listings]
    LOG.info("KAP directory: %s codes; %s are Borsa equities, %s non-equity codes (debt/lease issuers) dropped",
             len(directory), len(companies), len(directory) - len(companies))
    if len(companies) < args.min_companies:
        raise ValueError(f"Only {len(companies)} equities matched; aborting")

    failed: list[str] = []
    if not args.skip_details:
        cache = PageCache(http, args.cache_dir, args.cache_days)
        companies, failed = add_details(companies, cache, args.directory_url, args.cooldown_minutes * 60,
                                        args.max_cooldowns)

    LOG.info("Filled: IpoDate %s/%s (%s from first trading day), SectorName %s/%s, ReportingCurrency %s/%s",
             sum(bool(ipo_date(c)[0]) for c in companies), len(companies),
             sum(ipo_date(c)[1] == "first_trading_day" for c in companies),
             sum(c.sector_name is not None for c in companies), len(companies),
             sum(c.reporting_currency is not None for c in companies), len(companies))
    if failed and not args.allow_incomplete:
        LOG.error("KAP details missing for %s stocks (%s%s). %s was NOT written. "
                  "Rerun the same command later; fetched pages are cached and reused. "
                  "Use --allow-incomplete to write anyway.",
                  len(failed), ", ".join(failed[:10]), ", ..." if len(failed) > 10 else "", args.output)
        return 2
    if failed:
        LOG.warning("Writing incomplete output: %s stocks lack KAP details", len(failed))
    write_csv(companies, args.output, args.market_id)
    LOG.info("Saved %s stocks to %s", len(companies), args.output)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m stock_crawler companies", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", type=Path, default=OUTPUT_CSV, help="(default: %(default)s)")
    ap.add_argument("--market-id", type=int, help="Your database MarketId for Borsa İstanbul (written to every row)")
    ap.add_argument("--directory-url", default=DIRECTORY_URL,
                    help="KAP BIST companies directory page (default: %(default)s)")
    ap.add_argument("--listing-url", default=LISTING_URL,
                    help="Borsa İstanbul listing workbook, ilkislem.zip (default: %(default)s)")
    ap.add_argument("--skip-details", action="store_true",
                    help="Skip KAP profile/financial pages (no SectorName/ReportingCurrency; ~1 minute instead of ~45)")
    ap.add_argument("--proxy", help="HTTP or SOCKS5 proxy for all requests, e.g. http://127.0.0.1:12334")
    ap.add_argument("--fetch-backend", choices=("auto", "python", "curl"), default="auto",
                    help="HTTP client; auto switches to curl if Python's client cannot connect")
    ap.add_argument("--ca-bundle", help="PEM file of trusted CAs for the Python client (e.g. a corporate/VPN root)")
    ap.add_argument("--request-interval", type=float, default=2.0,
                    help="Normal seconds between requests, +/-30%% jitter (default: 2)")
    ap.add_argument("--max-request-interval", type=float, default=60.0,
                    help="Upper limit when slowing down after push-back from the server (default: 60)")
    ap.add_argument("--cooldown-minutes", type=float, default=5.0,
                    help="Pause when KAP keeps refusing requests, then retry (default: 5)")
    ap.add_argument("--max-cooldowns", type=int, default=3,
                    help="Stop after this many cool-downs in a row without success (default: 3)")
    ap.add_argument("--cache-dir", type=Path, default=Path("cache/companies"), help="(default: %(default)s)")
    ap.add_argument("--cache-days", type=float, default=7.0,
                    help="Reuse cached KAP pages for this many days (default: 7; 0 = refetch all)")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="Write the CSV even if some KAP pages could not be fetched")
    ap.add_argument("--min-companies", type=int, default=100, help="Abort if fewer stocks are found")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.proxy and args.fetch_backend == "python":
        ap.error("--proxy uses curl; choose --fetch-backend auto or curl")
    if args.proxy and not args.proxy.startswith(("http://", "https://", "socks5://", "socks5h://")):
        ap.error("--proxy must be an http://, https://, socks5:// or socks5h:// URL")
    if not 0.5 <= args.request_interval <= 120:
        ap.error("--request-interval must be between 0.5 and 120 seconds")
    if args.max_request_interval < args.request_interval:
        ap.error("--max-request-interval must be >= --request-interval")
    if args.cooldown_minutes < 0 or args.max_cooldowns < 0:
        ap.error("--cooldown-minutes and --max-cooldowns must not be negative")
    if not 0 <= args.cache_days <= 365:
        ap.error("--cache-days must be between 0 and 365")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        LOG.error("Interrupted; %s not written. Cached pages are kept for the next run.", args.output)
        return 130
    except (RuntimeError, ValueError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
