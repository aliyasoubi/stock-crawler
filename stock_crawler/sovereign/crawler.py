"""Türkiye sovereign macro data -> MacroSovereign CSV. Run: python -m stock_crawler sovereign"""
from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import requests

from .evds_client import BASE_URL as EVDS_URL, EvdsClient, EvdsError
from .fields import FIELDS, PeriodType
from .pipeline import build_rows, coverage_report
from .sources import CsvSource, EmptySource, EvdsSource
from .treasury import PAGES_API as TREASURY_URL, TreasuryDebtSource, TreasuryError
from .writers import write_csv

log = logging.getLogger("sovereign")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m stock_crawler sovereign",
                                description="Fetch Türkiye sovereign macro data into the MacroSovereign schema.")
    p.add_argument("--market-id", type=int, required=True, help="MarketId for Türkiye in your master data")
    p.add_argument("--years", type=int, default=5, help="full calendar years of history before the current year (default 5)")
    p.add_argument("--start", type=date.fromisoformat, help="override start date (YYYY-MM-DD)")
    p.add_argument("--end", type=date.fromisoformat, default=date.today(), help="as-of date (default today)")
    p.add_argument("--periods", nargs="+", choices=[p.value for p in PeriodType],
                   default=[p.value for p in PeriodType], help="period types to produce")
    p.add_argument("--output", type=Path, default=Path("output/sovereign/macro_sovereign_tr.csv"),
                   help="CSV output path (default: %(default)s)")
    p.add_argument("--cds-csv", help="CSV with daily Türkiye 5Y CDS (columns: date,value)")
    p.add_argument("--evds-url", default=EVDS_URL, help="CBRT EVDS data API (default: %(default)s)")
    p.add_argument("--treasury-url", default=TREASURY_URL,
                   help="Treasury (hmb.gov.tr) pages API, for the debt workbook (default: %(default)s)")
    p.add_argument("--ca-bundle", help="CA bundle path (default: OS trust store via truststore)")
    p.add_argument("--list-fields", action="store_true", help="print the field -> series mapping and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.years < 1:
        p.error("--years must be at least 1")
    return args


def use_os_trust_store() -> None:
    """Verify TLS against the OS trust store (handles corporate proxies and
    python.org builds whose bundled CA file is missing)."""
    try:
        import truststore
    except ImportError:
        log.debug("truststore not installed; using certifi bundle")
        return
    truststore.inject_into_ssl()


def print_fields() -> None:
    for f in FIELDS:
        print(f"{f.column} [{f.unit}] decimal({f.precision},{f.scale})")
        for s in f.series:
            where = f"EVDS {s.datagroup}" if s.source == "evds" else f"source={s.source}"
            print(f"    {s.code:<26} {s.native_freq.name:<12} agg={s.agg.value:<4} {where}  - {s.description}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_fields:
        print_fields()
        return 0

    if not args.ca_bundle:
        use_os_trust_store()
    start = args.start or date(args.end.year - args.years, 1, 1)
    verify = args.ca_bundle or True
    sources = {
        "evds": EvdsSource(EvdsClient(verify=verify, base_url=args.evds_url)),
        "treasury": TreasuryDebtSource(verify=verify, pages_api=args.treasury_url),
        "cds": CsvSource(args.cds_csv) if args.cds_csv else EmptySource("pass --cds-csv"),
    }

    log.info("Fetching %s..%s for MarketId=%s, periods=%s", start, args.end, args.market_id, args.periods)
    try:
        rows = build_rows(
            sources, args.market_id, [PeriodType(p) for p in args.periods],
            start=start, end=args.end, publish_date=date.today(),
        )
    except (EvdsError, TreasuryError, requests.RequestException, OverflowError, FileNotFoundError, KeyError) as exc:
        log.error("Aborted: %s", exc)
        return 1

    if not rows:
        log.error("No rows produced")
        return 1
    log.info("Values found per column:\n%s", coverage_report(rows))
    log.info("Wrote %d rows to %s", len(rows), write_csv(rows, args.output))
    return 0
