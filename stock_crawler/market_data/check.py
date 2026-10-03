"""Check a market_data.py CSV for errors and suspicious values.

Errors (exit code 1) mean the file is broken: bad header, unreadable numbers or dates,
duplicate or unsorted rows, impossible prices (Low > High, Close outside Low..High, ...),
USD values that do not match UsdTry.

Warnings are real market events or source quirks worth a look: big moves on days without a
corporate-action flag, PreviousClose that differs from the prior row's Close, days with far
fewer stocks than usual, tickers from companies.csv without data.

Run: python -m stock_crawler check_market_data
Python 3.10+ standard library only.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date
from pathlib import Path
from statistics import median
import sys

from .crawler import COMPANIES_CSV, CSV_FIELDS, OUTPUT_CSV, USD_FIELDS, read_tickers

PRICES = ["Open", "High", "Low", "Close", "Vwap"]
TOLERANCE = 0.011  # prices and percentages are rounded to 2-3 decimals in the bulletin


class Report:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.counts: Counter[str] = Counter()
        self.examples: dict[str, list[str]] = defaultdict(list)
        self.errors: set[str] = set()

    def add(self, check: str, example: str, error: bool = False) -> None:
        self.counts[check] += 1
        if error:
            self.errors.add(check)
        if len(self.examples[check]) < self.limit:
            self.examples[check].append(example)

    def print(self) -> None:
        for kind, checks in (("ERROR", self.errors), ("WARNING", self.counts.keys() - self.errors)):
            for check in sorted(checks, key=lambda c: -self.counts[c]):
                print(f"{kind:7} {self.counts[check]:>7}  {check}")
                for example in self.examples[check]:
                    print(f"{'':17}{example}")


def check(path: Path, companies: Path | None, move_limit: float, report: Report) -> dict[str, object]:
    stocks_per_day: Counter[str] = Counter()
    tickers: dict[str, list[str]] = {}  # ticker -> [first date, last date, rows]
    previous: dict[str, dict[str, str]] = {}
    last_key = ("", "")
    rows = 0
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        header = reader.fieldnames or []
        usd = header == CSV_FIELDS + USD_FIELDS
        if header not in (CSV_FIELDS, CSV_FIELDS + USD_FIELDS):
            report.add("unexpected header", ",".join(header), error=True)
            return {"rows": 0}
        for line, row in enumerate(reader, start=2):
            rows += 1
            where = f"line {line}: {row['Ticker']} {row['Date']}"
            try:
                date.fromisoformat(row["Date"])
                p = {name: float(row[name]) for name in PRICES}
                # Empty on a stock's first trading day (IPO): there is no previous close.
                p["PreviousClose"] = float(row["PreviousClose"]) if row["PreviousClose"] else 0.0
                volume, value, change = float(row["Volume"]), float(row["TradedValue"]), float(row["ChangePercent"])
            except ValueError as exc:
                report.add("unreadable date or number", f"{where}: {exc}", error=True)
                continue

            key = (row["Ticker"], row["Date"])
            if key == last_key:
                report.add("duplicate ticker/date", where, error=True)
            elif key < last_key:
                report.add("rows not sorted by ticker, date", where, error=True)
            last_key = key
            stocks_per_day[row["Date"]] += 1
            first, _, count = tickers.setdefault(row["Ticker"], [row["Date"], row["Date"], 0])
            tickers[row["Ticker"]] = [first, row["Date"], count + 1]

            # Prices
            if min(p["Open"], p["High"], p["Low"], p["Close"]) <= 0:
                report.add("price <= 0", where, error=True)
            if p["Low"] > p["High"] or not (p["Low"] - TOLERANCE <= p["Open"] <= p["High"] + TOLERANCE) \
                    or not (p["Low"] - TOLERANCE <= p["Close"] <= p["High"] + TOLERANCE):
                report.add("Open/Close outside Low..High", f"{where}: O={p['Open']} H={p['High']} "
                           f"L={p['Low']} C={p['Close']}", error=True)
            if not p["Low"] - TOLERANCE <= p["Vwap"] <= p["High"] + TOLERANCE:
                report.add("VWAP outside Low..High", f"{where}: VWAP={p['Vwap']} L={p['Low']} H={p['High']}")
            if volume <= 0 or value <= 0:
                report.add("zero volume or traded value", where, error=True)
            elif abs(value / volume - p["Vwap"]) > max(TOLERANCE, p["Vwap"] * 0.01):
                report.add("TradedValue/Volume differs from VWAP by >1%",
                           f"{where}: {value / volume:.4f} vs {p['Vwap']}")

            # Day-to-day consistency
            if not row["PreviousClose"]:
                report.add("no PreviousClose (first trading day, e.g. IPO)", where)
            elif p["PreviousClose"] > 0:
                expected = (p["Close"] / p["PreviousClose"] - 1) * 100
                if abs(expected - change) > max(0.05, abs(change) * 0.001):
                    report.add("ChangePercent differs from Close/PreviousClose",
                               f"{where}: file {change}, computed {expected:.3f}")
                move = abs(expected)
                if move > move_limit and not row["CorporateAction"]:
                    report.add(f"move > {move_limit:g}% without CorporateAction flag",
                               f"{where}: {p['PreviousClose']} -> {p['Close']} ({expected:+.1f}%)")
            before = previous.get(row["Ticker"])
            if before and row["PreviousClose"] and abs(float(before["Close"]) - p["PreviousClose"]) > TOLERANCE \
                    and not row["CorporateAction"]:
                report.add("PreviousClose differs from prior row's Close (no corporate action)",
                           f"{where}: prior {before['Date']} Close={before['Close']}, "
                           f"PreviousClose={p['PreviousClose']}")
            if row["CorporateAction"]:
                report.add("rows with a CorporateAction flag (prices not adjusted)",
                           f"{where}: code {row['CorporateAction']}, {p['PreviousClose']} -> {p['Close']}")
            previous[row["Ticker"]] = row

            # USD
            if usd:
                if not row["UsdTry"]:
                    report.add("missing UsdTry", where)
                else:
                    rate = float(row["UsdTry"])
                    if not 1 < rate < 1000:
                        report.add("UsdTry out of range", f"{where}: {rate}", error=True)
                    elif abs(float(row["CloseUsd"]) - p["Close"] / rate) > max(1e-6, p["Close"] / rate * 1e-4):
                        report.add("CloseUsd != Close / UsdTry", where, error=True)

    if not rows:
        report.add("no data rows", str(path), error=True)
        return {"rows": 0}

    typical = median(stocks_per_day.values())
    for day, count in sorted(stocks_per_day.items()):
        if count < typical * 0.5:
            report.add("days with < 50% of the usual number of stocks", f"{day}: {count} (usual {typical:.0f})")
    if companies:
        for ticker in sorted(read_tickers(companies) - tickers.keys()):
            report.add(f"tickers in {companies.name} without data", ticker)
    days = sorted(stocks_per_day)
    return {"rows": rows, "tickers": len(tickers), "days": len(days), "first": days[0], "last": days[-1],
            "typical": typical, "per_ticker": tickers, "usd": usd}


def check_split(directory: Path, main: Path, report: Report) -> int:
    """Each per-ticker file must equal that ticker's rows in the main CSV."""
    by_ticker: dict[str, list[str]] = defaultdict(list)
    with main.open(encoding="utf-8", newline="") as source:
        header = source.readline()
        for line in source:
            by_ticker[line.split(",", 1)[0]].append(line)
    files = {p.stem: p for p in directory.glob("*.csv")}
    for ticker in sorted(by_ticker.keys() - files.keys()):
        report.add("per-ticker file missing", ticker, error=True)
    for ticker in sorted(files.keys() - by_ticker.keys()):
        report.add("per-ticker file without rows in main CSV (stale?)", ticker)
    for ticker in sorted(files.keys() & by_ticker.keys()):
        with files[ticker].open(encoding="utf-8", newline="") as source:
            if source.readline() != header or source.readlines() != by_ticker[ticker]:
                report.add("per-ticker file differs from main CSV", ticker, error=True)
    return len(files)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m stock_crawler check_market_data", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, nargs="?", default=OUTPUT_CSV, help="(default: %(default)s)")
    ap.add_argument("--companies", type=Path, default=COMPANIES_CSV,
                    help="Report tickers from this file that have no data (default: %(default)s)")
    ap.add_argument("--split-dir", type=Path, help="Also check the per-ticker files from --split-dir")
    ap.add_argument("--move-limit", type=float, default=25.0,
                    help="Warn about daily moves above this %% without a corporate action (default: 25)")
    ap.add_argument("--examples", type=int, default=5, help="Examples shown per finding (default: 5)")
    ap.add_argument("--ticker", help="Also print a summary of this ticker")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report(args.examples)
    try:
        stats = check(args.csv, args.companies if args.companies.exists() else None, args.move_limit, report)
        split_files = check_split(args.split_dir, args.csv, report) if args.split_dir and stats["rows"] else None
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"File      {args.csv}")
    if stats["rows"]:
        print(f"Rows      {stats['rows']:,}")
        print(f"Stocks    {stats['tickers']:,}")
        print(f"Days      {stats['days']:,} trading days, {stats['first']} to {stats['last']}, "
              f"usually {stats['typical']:.0f} stocks per day")
        print(f"USD       {'yes' if stats['usd'] else 'no (--no-usd)'}")
        if split_files is not None:
            print(f"Split     {split_files:,} per-ticker files in {args.split_dir}")
        if args.ticker:
            info = stats["per_ticker"].get(args.ticker)
            print(f"{args.ticker:9} " + (f"{info[2]:,} rows, {info[0]} to {info[1]}" if info else "no rows"))
    print()
    report.print()
    print()
    if report.errors:
        print(f"FAILED: {len(report.errors)} kinds of error")
        return 1
    print("OK: no errors" + (f" ({len(report.counts)} kinds of warning to review)" if report.counts else ""))
    return 0
