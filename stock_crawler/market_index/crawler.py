"""Borsa Istanbul (BIST) daily index closes -> Market, MarketIndexMaster and MarketIndexData CSVs.

Prices come from the public index chart endpoint used by isyatirim.com.tr
(15-minute delayed; history back to 2000 for XU100). One request per index
covers the whole date range, so all 54 indices take 54 requests. Closes are
copied exactly as the endpoint writes them.

Config: config/markets.csv (Market) and config/indices.csv (MarketIndexMaster);
edit both so the IDs match your database.

Politeness / anti-ban measures:
  * requests are sequential, spaced by --delay seconds plus random jitter;
  * 429/5xx are retried with exponential backoff, honouring Retry-After;
  * a 401/403 or an HTML page instead of JSON (firewall/WAF block) stops the
    run immediately instead of hammering the server;
  * the run also stops after --max-failures consecutive failed indices.

Exit codes: 0 ok, 1 some indices failed, 2 config error, 3 run aborted.

Examples:
    python -m stock_crawler market_index                            # all indices, last --years years
    python -m stock_crawler market_index -i XU100 XU030 --start 2020-01-01
    python -m stock_crawler market_index --start 2026-01-01 --end 2026-06-30 --out-dir exports/h1
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time as dtime
from decimal import Decimal
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from ..dates import years_start
from .export import DATA_FILE, MARKET_FILE, MASTER_FILE, write_data_csv, write_market_csv, write_master_csv
from .http_client import BlockedError, Throttle, build_session, get_json
from .models import IndexInfo, MarketIndexData, load_index_map, load_markets

SOURCE_URL = (
    "https://www.isyatirim.com.tr/_Layouts/15/IsYatirim.Website/Common/"
    "ChartData.aspx/IndexHistoricalAll"
)
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/Endeksler.aspx",
}
DAILY_PERIOD = 1440  # minutes per bar
ISTANBUL = ZoneInfo("Europe/Istanbul")
# Continuous trading ends 18:00 and the closing auction settles shortly after;
# before this local time today's value is intraday, not a close.
CLOSE_SETTLED = dtime(18, 15)

log = logging.getLogger("market_index")


def fetch_index(
    session: requests.Session, url: str, index: IndexInfo, start: date, end: date
) -> list[MarketIndexData]:
    params = {
        "period": DAILY_PERIOD,
        "from": start.strftime("%Y%m%d000000"),
        "to": end.strftime("%Y%m%d235959"),
        "endeks": index.code,
    }
    points = get_json(session, url, params, index.code).get("data") or []

    rows = []
    for ts_ms, close in points:
        if close is None:
            continue
        # Bars are stamped at midnight Istanbul time; convert in that zone
        # (Turkey used +02:00/DST until 2016, so a fixed offset is wrong).
        trade_date = datetime.fromtimestamp(float(ts_ms) / 1000, tz=ISTANBUL).date()
        # BIST has no weekend sessions; the source occasionally repeats
        # Friday's close on a Saturday bar (e.g. XBANK 2015-11-28).
        if trade_date.weekday() >= 5:
            continue
        if start <= trade_date <= end:
            rows.append(MarketIndexData(trade_date, index.index_id, Decimal(close)))
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    today = datetime.now(ISTANBUL).date()
    p = argparse.ArgumentParser(prog="python -m stock_crawler market_index",
                                description="Crawl Borsa Istanbul daily index closes into CSV.")
    p.add_argument("-i", "--indices", nargs="+", metavar="CODE",
                   help="index codes to fetch (default: all in --indices-csv)")
    p.add_argument("--years", type=int, default=5, help="years of history: from 1 January, that many years before --end's year (default: 5)")
    p.add_argument("--start", type=date.fromisoformat,
                   help="first trade date, YYYY-MM-DD (overrides --years)")
    p.add_argument("--end", type=date.fromisoformat, default=today,
                   help="last trade date, YYYY-MM-DD (default: today)")
    p.add_argument("--out-dir", type=Path, default=Path("output/market_index"),
                   help=f"directory for {MARKET_FILE}, {MASTER_FILE} and {DATA_FILE} (default: %(default)s)")
    p.add_argument("--markets-csv", type=Path, default=Path("config/markets.csv"),
                   help="rows for the Market table (default: %(default)s)")
    p.add_argument("--indices-csv", type=Path, default=Path("config/indices.csv"),
                   help="rows for the MarketIndexMaster table (default: %(default)s)")
    p.add_argument("--source-url", default=SOURCE_URL, help="index history endpoint (default: %(default)s)")
    p.add_argument("--include-today", action="store_true",
                   help="keep today's row even before the market close has settled")
    p.add_argument("--delay", type=float, default=1.5,
                   help="minimum seconds between requests (default: 1.5)")
    p.add_argument("--jitter", type=float, default=1.0,
                   help="extra random 0..N seconds added to each delay (default: 1.0)")
    p.add_argument("--max-failures", type=int, default=3,
                   help="abort after this many consecutive failed indices (default: 3)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.years < 1:
        p.error("--years must be at least 1")
    args.start = args.start or years_start(args.end, args.years)
    if args.start > args.end:
        p.error("--start must be on or before --end")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        markets = load_markets(args.markets_csv)
        index_map = load_index_map(args.indices_csv)
    except (OSError, ValueError, KeyError) as exc:  # KeyError: a missing column
        log.error("bad config file: %s", exc)
        return 2
    orphans = sorted({i.code for i in index_map.values() if i.market_id not in markets})
    if orphans:
        log.error("indices with a MarketId missing from %s: %s", args.markets_csv, ", ".join(orphans))
        return 2
    codes = list(dict.fromkeys(c.upper() for c in args.indices)) if args.indices else list(index_map)
    unknown = [c for c in codes if c not in index_map]
    if unknown:
        log.error("codes not in %s: %s — add them with an IndexId first", args.indices_csv, ", ".join(unknown))
        return 2

    now = datetime.now(ISTANBUL)
    drop_today = not args.include_today and now.time() < CLOSE_SETTLED

    log.info("%s to %s: %d indices from %s", args.start, args.end, len(codes), args.source_url)
    session = build_session(HEADERS)
    throttle = Throttle(args.delay, args.jitter)
    rows: dict[tuple[date, int], MarketIndexData] = {}
    failed: list[str] = []
    consecutive_failures = 0
    aborted = False
    for n, code in enumerate(codes, 1):
        index = index_map[code]
        throttle.wait()
        try:
            fetched = fetch_index(session, args.source_url, index, args.start, args.end)
        except BlockedError as exc:
            log.error("blocked by the server (%s); stopping so the IP is not banned. "
                      "Wait before retrying, or raise --delay.", exc)
            failed.append(code)
            aborted = True
            break
        except (requests.RequestException, ValueError) as exc:
            log.warning("[%d/%d] %s failed: %s", n, len(codes), code, exc)
            failed.append(code)
            consecutive_failures += 1
            if consecutive_failures >= args.max_failures:
                log.error("%d consecutive failures; stopping.", consecutive_failures)
                aborted = True
                break
            continue
        consecutive_failures = 0
        if drop_today:
            fetched = [r for r in fetched if r.trade_date != now.date()]
        for r in fetched:
            rows[(r.trade_date, r.index_id)] = r  # enforce the (TradeDate, IndexId) PK
        log.info("[%d/%d] %s (%s): %d rows", n, len(codes), code, index.name, len(fetched))

    market_path = args.out_dir / MARKET_FILE
    master_path = args.out_dir / MASTER_FILE
    data_path = args.out_dir / DATA_FILE
    # Each file holds every parent its child rows reference, so FKs always resolve.
    selected = [index_map[c] for c in codes]
    selected_markets = [markets[m] for m in {i.market_id for i in selected}]
    write_market_csv(selected_markets, market_path)
    write_master_csv(selected, master_path)
    write_data_csv([rows[k] for k in sorted(rows)], data_path)
    log.info("wrote %d markets to %s", len(selected_markets), market_path)
    log.info("wrote %d indices to %s", len(codes), master_path)
    log.info("wrote %d rows to %s", len(rows), data_path)

    if aborted:
        skipped = codes[codes.index(failed[-1]) + 1:]
        log.warning("run aborted; failed: %s; not attempted: %d indices",
                    ", ".join(failed), len(skipped))
        return 3
    if failed:
        log.warning("failed indices: %s", ", ".join(failed))
        return 1
    return 0
