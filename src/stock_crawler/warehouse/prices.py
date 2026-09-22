"""No-login daily BIST snapshot ingestion from İş Yatırım's public page feed.

This adapter is deliberately limited to the current/latest trading-day snapshot.
It is not a historical backfill API and it does not manufacture turnover from
closing price multiplied by volume.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import time as clock
from zoneinfo import ZoneInfo

import httpx

from ..core.storage import sha256_bytes, utcnow, write_atomic
from .loader import bundle


ENDPOINT = "https://www.isyatirim.com.tr/_layouts/15/Isyatirim.Website/Common/Data.aspx/OneEndeks"
PROVIDER = "isyatirim_public_daily"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
SYMBOL = re.compile(r"[A-Z0-9]{1,20}")
ISTANBUL = ZoneInfo("Europe/Istanbul")


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
