"""No-login BIST price ingestion from İş Yatırım's public page feeds.

Two separate products, deliberately kept apart:

* ``build_isyatirim_daily`` reads the current/latest trading-day snapshot
  (``OneEndeks``). It reports open, high, low, close, share quantity and TRY turnover.
* ``build_isyatirim_history`` reads the daily history of one symbol (``HisseTekil``).
  It reports NO opening price and NO share quantity, so backfilled rows carry a NULL
  OpenPrice and a Volume derived from turnover / VWAP.

Neither manufactures turnover from closing price multiplied by volume.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
import time as clock
from zoneinfo import ZoneInfo

import httpx

from ..core.storage import sha256_bytes, utcnow, write_atomic
from .loader import bundle


ENDPOINT = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/OneEndeks"
HISTORY_ENDPOINT = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/HisseTekil"
PROVIDER = "isyatirim_public_daily"
HISTORY_PROVIDER = "isyatirim_public_history"
HISTORY_VERSION = "warehouse-isyatirim-history-1.0.0"
HISTORY_HOSTS = {"www.isyatirim.com.tr", "isyatirim.com.tr"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
# One symbol's full history is large: ASELS 2015-2026 is 2,939 rows / ~2 MB.
MAX_HISTORY_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_HISTORY_ROWS = 20000
SYMBOL = re.compile(r"[A-Z0-9]{1,20}")
ISTANBUL = ZoneInfo("Europe/Istanbul")

# HisseTekil serves two parallel series per row. HG_* is the raw as-traded series: it was
# verified against the row's own market cap (HG_KAPANIS == PD / SERMAYE exactly, on every
# sampled row for ASELS, THYAO and GARAN). HGDG_* is back-adjusted for bonus issues and
# splits and drifts from the traded price the further back you read (ASELS 2015-01-02:
# 1.42 adjusted vs 12.00 traded). The warehouse contract requires price_basis="as_traded",
# so ONLY the HG_* fields may be mapped. The date and symbol carry the HGDG_ prefix but are
# not part of either price series.
HISTORY_DATE = "HGDG_TARIH"
HISTORY_SYMBOL = "HGDG_HS_KODU"
HISTORY_CLOSE = "HG_KAPANIS"
HISTORY_LOW = "HG_MIN"
HISTORY_HIGH = "HG_MAX"
HISTORY_VWAP = "HG_AOF"
HISTORY_TURNOVER = "HG_HACIM"


def _decimal(value, field, *, positive=False, nonnegative=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}: finite decimal required") from exc
    if not number.is_finite():
        raise ValueError(f"{field}: finite decimal required")
    if positive and number <= 0:
        raise ValueError(f"{field}: positive value required")
    if nonnegative and number < 0:
        raise ValueError(f"{field}: nonnegative value required")
    return number


def _integer(value, field):
    number = _decimal(value, field, nonnegative=True)
    if number != number.to_integral_value():
        raise ValueError(f"{field}: integral share quantity required")
    return int(number)


def _source_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("updateDate: ISO timestamp required") from exc
    if parsed.tzinfo is None:
        raise ValueError("updateDate: timezone required")
    return parsed


def _archive_response(data, root):
    digest = sha256_bytes(data)
    write_atomic(Path(root) / "raw" / PROVIDER / digest / "source.json", data)
    return digest


def _validate_codes(codes):
    normalized = []
    for code in codes:
        code = str(code).strip().upper()
        if not SYMBOL.fullmatch(code):
            raise ValueError(f"invalid BIST symbol {code!r}")
        if code not in normalized:
            normalized.append(code)
    return normalized


def _chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def fetch_isyatirim_snapshots(codes, *, archive_dir, batch_size=20,
                              pause_seconds=1.0, client=None, observed_at=None):
    """Fetch fixed-host JSON batches and return symbol -> row/hash metadata.

    A throttle or server error stops the run instead of continuing a request
    storm. Missing individual symbols are reported by the caller.
    """
    codes = _validate_codes(codes)
    if not 1 <= int(batch_size) <= 20:
        raise ValueError("batch_size must be between 1 and 20")
    if not 0 <= float(pause_seconds) <= 60:
        raise ValueError("pause_seconds must be between 0 and 60")
    captured = observed_at or utcnow().isoformat()
    own = client is None
    client = client or httpx.Client(timeout=40, follow_redirects=False)
    snapshots = {}
    try:
        batches = list(_chunks(codes, int(batch_size)))
        for number, requested in enumerate(batches, 1):
            response = client.get(ENDPOINT, params={"endeks": ",".join(requested)}, headers={
                "User-Agent": "StockCrawlerWarehouse/1.0",
                "Accept": "application/json",
            })
            if response.status_code in (429, 503):
                raise ValueError(f"İş Yatırım throttled the request ({response.status_code}); stop and retry later")
            if response.status_code != 200:
                raise ValueError(f"İş Yatırım returned HTTP {response.status_code}; run stopped")
            data = response.content
            if not data or len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("İş Yatırım returned an empty or oversized response")
            try:
                rows = json.loads(data.decode("utf-8-sig"), parse_float=Decimal)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("İş Yatırım response is not valid JSON") from exc
            if isinstance(rows, dict) and isinstance(rows.get("error"), dict):
                error = rows["error"]
                raise ValueError(f"İş Yatırım rejected the batch: {error.get('code', 'unknown')} - {error.get('message', 'unspecified')}")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("İş Yatırım response must be a JSON row array")
            digest = _archive_response(data, archive_dir)
            requested_set = set(requested)
            for row in rows:
                symbol = str(row.get("symbol", "")).strip().upper()
                if symbol not in requested_set:
                    raise ValueError(f"unexpected symbol {symbol!r} in İş Yatırım response")
                if symbol in snapshots:
                    raise ValueError(f"duplicate symbol {symbol!r} in İş Yatırım response")
                snapshots[symbol] = {"row": row, "raw_sha256": digest,
                                     "observed_at": captured, "batch": number}
            if number < len(batches) and pause_seconds:
                clock.sleep(float(pause_seconds))
    finally:
        if own:
            client.close()
    return snapshots


def build_listing_status(*, company_map, names, market_id, archive_dir=Path("data/warehouse"),
                         batch_size=20, pause_seconds=1.0, client=None, observed_at=None):
    """Company rows carrying IsActive evidence taken from the public quote feed.

    A live quote is positive evidence that a ticker is a listed, tradeable equity. The
    absence of one is NOT proof of delisting: most unquoted KAP registrants are bond,
    sukuk, factoring or leasing issuers that never had listed stock, and a listed name can
    also be suspended on the survey day. Unquoted companies are therefore left with
    IsActive unset rather than written as inactive, and are returned for review instead.
    """
    company_map = {str(k).upper(): int(v) for k, v in company_map.items()}
    codes = _validate_codes(company_map)
    if not codes:
        raise ValueError("company_map is empty; seed dbo.Company and export the ID map first")
    captured = observed_at or utcnow().isoformat()
    snapshots = fetch_isyatirim_snapshots(codes, archive_dir=archive_dir, batch_size=batch_size,
                                          pause_seconds=pause_seconds, client=client,
                                          observed_at=captured)
    records, quoted, unquoted = [], [], []
    for code in codes:
        captured_row = snapshots.get(code)
        if captured_row is None or not captured_row["row"].get("last"):
            unquoted.append(code)
            continue
        quoted.append(code)
        records.append({"table": "Company", "values": {
            "CompanyId": company_map[code],
            "Ticker": code,
            "MarketId": int(market_id),
            "FullName": names[code],
            "IsActive": 1,
        }, "source": {
            "provider": PROVIDER,
            "observed_at": captured,
            "raw_sha256": captured_row["raw_sha256"],
            "source_url": ENDPOINT,
            "parser_version": "warehouse-listing-status-1.0.0",
            "symbol": code,
            "evidence": "live quote carrying a last traded price on the survey date",
            "field_semantics": {
                "IsActive": "1 = the exchange feed quoted this ticker; absence of a quote is "
                            "not written back as 0, because it does not distinguish a delisting "
                            "from a registrant that never had listed stock",
            },
        }})
    result = bundle(records)
    result["listing_survey"] = {
        "observed_at": captured, "companies_surveyed": len(codes),
        "quoted": len(quoted), "unquoted": len(unquoted),
        "unquoted_tickers": unquoted,
        "note": "Unquoted companies keep IsActive NULL. Review them before treating the "
                "quoted set as the complete listed-equity universe.",
    }
    return result


def _history_date(value):
    """HGDG_TARIH is rendered DD-MM-YYYY, unlike the ISO timestamps in the daily feed."""
    try:
        day, month, year = str(value).split("-")
        return date(int(year), int(month), int(day))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{HISTORY_DATE}: DD-MM-YYYY required, got {value!r}") from exc


def history_url(symbol, start, end):
    symbol = _validate_codes([symbol])[0]
    if end < start:
        raise ValueError("end date precedes start date")
    return (f"{HISTORY_ENDPOINT}?hisse={symbol}"
            f"&startdate={start.strftime('%d-%m-%Y')}&enddate={end.strftime('%d-%m-%Y')}")


def fetch_isyatirim_history(symbol, *, start, end, fetcher, archive_dir):
    """One paced GET for one symbol's daily history. Returns (rows, raw_sha256).

    `fetcher` is a PacedClient: the whole backfill is hundreds of single-symbol requests,
    so it needs the budget, host cooldown and Retry-After handling the daily snapshot
    (a handful of batched requests) can do without.
    """
    result = fetcher.get(history_url(symbol, start, end),
                         accept="application/json, text/javascript, */*; q=0.01")
    data = result.content
    if not data or len(data) > MAX_HISTORY_RESPONSE_BYTES:
        raise ValueError("İş Yatırım returned an empty or oversized history response")
    try:
        payload = json.loads(data.decode("utf-8-sig"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("İş Yatırım history response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("İş Yatırım history response must be a JSON object")
    if payload.get("ok") is not True or payload.get("errorCode"):
        raise ValueError(f"İş Yatırım rejected the history request: "
                         f"{payload.get('errorCode')} - {payload.get('errorDescription')}")
    rows = payload.get("value")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("İş Yatırım history payload carries no row array")
    if len(rows) > MAX_HISTORY_ROWS:
        raise ValueError(f"history response exceeds {MAX_HISTORY_ROWS} rows; narrow the date range")
    digest = sha256_bytes(data)
    write_atomic(Path(archive_dir) / "raw" / HISTORY_PROVIDER / digest / "source.json", data)
    return rows, digest


def build_isyatirim_history(symbol, company_id, *, start, end, fetcher,
                            archive_dir=Path("data/warehouse"), source_priority=3,
                            observed_at=None):
    """MarketData rows for one symbol from the daily-history feed.

    Two documented departures from the daily snapshot, both recorded per row:
    the product reports no opening price, so OpenPrice is NULL; and it reports no share
    quantity, so Volume is derived as turnover / VWAP. That quotient is exact when AOF is
    a true volume-weighted average price, because turnover / shares is the definition of
    VWAP. Days whose AOF is zero yield no share count and are reported as errors rather
    than loaded with a guessed volume.
    """
    symbol = _validate_codes([symbol])[0]
    if not 1 <= int(source_priority) <= 255:
        raise ValueError("source_priority must be between 1 and 255")
    captured = observed_at or utcnow().isoformat()
    rows, digest = fetch_isyatirim_history(symbol, start=start, end=end,
                                           fetcher=fetcher, archive_dir=archive_dir)
    records, errors, seen = [], [], set()
    for row in rows:
        trade_date = None
        try:
            found = str(row.get(HISTORY_SYMBOL, "")).strip().upper()
            if found != symbol:
                raise ValueError(f"row reports symbol {found!r}, requested {symbol!r}")
            trade_date = _history_date(row.get(HISTORY_DATE))
            if not start <= trade_date <= end:
                raise ValueError("row falls outside the requested date range")
            if trade_date in seen:
                raise ValueError("duplicate trade date in provider response")
            seen.add(trade_date)
            close = _decimal(row.get(HISTORY_CLOSE), HISTORY_CLOSE, positive=True)
            high = _decimal(row.get(HISTORY_HIGH), HISTORY_HIGH, positive=True)
            low = _decimal(row.get(HISTORY_LOW), HISTORY_LOW, positive=True)
            turnover = _decimal(row.get(HISTORY_TURNOVER), HISTORY_TURNOVER, nonnegative=True)
            vwap = _decimal(row.get(HISTORY_VWAP), HISTORY_VWAP, nonnegative=True)
            if vwap <= 0:
                raise ValueError(f"{HISTORY_VWAP} is zero; share volume cannot be derived from turnover")
            volume = int((turnover / vwap).to_integral_value(rounding=ROUND_HALF_UP))
            records.append({"table": "MarketData", "values": {
                "TradeDate": trade_date.isoformat(),
                "CompanyId": int(company_id),
                "OpenPrice": None,
                "HighPrice": str(high),
                "LowPrice": str(low),
                "ClosePrice": str(close),
                "Volume": volume,
                "ValueTraded": str(turnover),
                "SourcePriority": int(source_priority),
            }, "source": {
                "provider": HISTORY_PROVIDER,
                "observed_at": captured,
                "raw_sha256": digest,
                "source_url": HISTORY_ENDPOINT,
                "parser_version": HISTORY_VERSION,
                "symbol": symbol,
                "currency": "TRY",
                "price_basis": "as_traded",
                "price_series": "HG_* raw traded series; HGDG_* back-adjusted series not used",
                "source_priority": int(source_priority),
                "snapshot_scope": "daily_history_replay",
                "field_semantics": {
                    "OpenPrice": "not reported by this product; the daily snapshot supplies it going forward",
                    "HighPrice": f"provider {HISTORY_HIGH}", "LowPrice": f"provider {HISTORY_LOW}",
                    "ClosePrice": f"provider {HISTORY_CLOSE}; equals PD / SERMAYE in the source row",
                    "ValueTraded": f"provider {HISTORY_TURNOVER}; actual TRY turnover",
                    "Volume": f"derived {HISTORY_TURNOVER} / {HISTORY_VWAP} (turnover / VWAP), rounded half-up; "
                              "the published VWAP carries three decimals, so the share count can differ "
                              "from the exchange figure by a few shares (~0.0001% on a 19.5M-share day)",
                },
                "derivations": {"Volume": {"method": "turnover_divided_by_vwap",
                                           "turnover": str(turnover), "vwap": str(vwap)}},
            }})
        except (ValueError, TypeError, ArithmeticError) as exc:
            errors.append({"symbol": symbol, "trade_date": trade_date.isoformat() if trade_date else None,
                           "error": str(exc)})
    return bundle(records, errors)


def build_isyatirim_daily(*, company_map=None, index_map=None, company_codes=None,
                          index_codes=None, archive_dir=Path("data/warehouse"),
                          source_priority=2, batch_size=20, pause_seconds=1.0,
                          allow_intraday=False, client=None, now=None):
    """Build MarketData and MarketIndexData rows from the latest public snapshot."""
    company_map = {str(k).upper(): int(v) for k, v in (company_map or {}).items()}
    index_map = {str(k).upper(): int(v) for k, v in (index_map or {}).items()}
    company_codes = _validate_codes(company_codes if company_codes is not None else company_map)
    index_codes = _validate_codes(index_codes if index_codes is not None else index_map)
    if not company_codes and not index_codes:
        raise ValueError("provide at least one company or index code")
    unknown_companies = sorted(set(company_codes) - set(company_map))
    unknown_indices = sorted(set(index_codes) - set(index_map))
    if unknown_companies or unknown_indices:
        raise ValueError(f"codes missing from exported ID maps: {unknown_companies + unknown_indices}")
    overlap = sorted(set(company_codes) & set(index_codes))
    if overlap:
        raise ValueError(f"codes cannot be both companies and indices: {overlap}")
    if not 1 <= int(source_priority) <= 255:
        raise ValueError("source_priority must be between 1 and 255")

    requested = company_codes + index_codes
    observed = utcnow().isoformat()
    snapshots = fetch_isyatirim_snapshots(requested, archive_dir=archive_dir,
        batch_size=batch_size, pause_seconds=pause_seconds, client=client,
        observed_at=observed)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    turkey_now = current.astimezone(ISTANBUL)
    records, errors = [], []
    for code in requested:
        captured = snapshots.get(code)
        if captured is None:
            errors.append({"symbol": code, "error": "symbol_missing_from_provider_response"})
            continue
        row = captured["row"]
        try:
            source_time = _source_datetime(row.get("updateDate"))
            source_day = source_time.astimezone(ISTANBUL).date()
            if source_day > turkey_now.date():
                raise ValueError("provider date is in the future")
            if (not allow_intraday and source_day == turkey_now.date()
                    and turkey_now.time() < time(18, 15)):
                raise ValueError("today's snapshot is intraday; rerun after 18:15 Europe/Istanbul")
            close = _decimal(row.get("last"), "last", positive=True)
            common_source = {
                "provider": PROVIDER,
                "observed_at": captured["observed_at"],
                "raw_sha256": captured["raw_sha256"],
                "source_url": ENDPOINT,
                "parser_version": "warehouse-isyatirim-daily-1.0.0",
                "currency": "TRY",
                "price_basis": "as_traded",
                "source_priority": int(source_priority),
                "source_update_time": source_time.isoformat(),
                "snapshot_scope": "latest_public_daily_snapshot",
            }
            if code in company_map:
                open_price = _decimal(row.get("open"), "open", positive=True)
                high = _decimal(row.get("high"), "high", positive=True)
                low = _decimal(row.get("low"), "low", positive=True)
                quantity = _integer(row.get("quantity"), "quantity")
                turnover = _decimal(row.get("volume"), "volume", nonnegative=True)
                records.append({"table": "MarketData", "values": {
                    "TradeDate": source_day.isoformat(),
                    "CompanyId": company_map[code],
                    "OpenPrice": str(open_price),
                    "HighPrice": str(high),
                    "LowPrice": str(low),
                    "ClosePrice": str(close),
                    "Volume": quantity,
                    "ValueTraded": str(turnover),
                    "SourcePriority": int(source_priority),
                }, "source": dict(common_source, symbol=code, field_semantics={
                    "Volume": "provider quantity; shares traded",
                    "ValueTraded": "provider volume; actual TRY turnover",
                })})
            else:
                records.append({"table": "MarketIndexData", "values": {
                    "TradeDate": source_day.isoformat(),
                    "IndexId": index_map[code],
                    "ClosePrice": str(close),
                }, "source": dict(common_source, symbol=code)})
        except (ValueError, TypeError) as exc:
            errors.append({"symbol": code, "error": str(exc)})
    return bundle(records, errors)
