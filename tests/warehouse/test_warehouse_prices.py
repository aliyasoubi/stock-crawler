from datetime import datetime, timezone
from decimal import Decimal
import json

import httpx
import pytest

from stock_crawler.warehouse.prices import build_isyatirim_daily, fetch_isyatirim_snapshots


def row(symbol, update="2026-09-22T18:20:00.000+03:00"):
    return {
        "updateDate": update,
        "open": Decimal("10.25"),
        "high": Decimal("11.50"),
        "low": Decimal("10.00"),
        "last": Decimal("11.00"),
        "quantity": Decimal("101"),
        "volume": Decimal("1099.75"),
        "symbol": symbol,
    }


def client_for(rows, status=200):
    def transport(request):
        assert request.url.host == "www.isyatirim.com.tr"
        return httpx.Response(status, text=json.dumps(rows, default=str))
    return httpx.Client(transport=httpx.MockTransport(transport))


def test_daily_maps_real_turnover_and_indices(tmp_path):
    with client_for([row("THYAO"), row("XU100")]) as client:
        result = build_isyatirim_daily(company_map={"THYAO": 7}, index_map={"XU100": 3},
            archive_dir=tmp_path, pause_seconds=0, client=client,
            now=datetime(2026, 9, 22, 19, 0, tzinfo=timezone.utc))
    assert not result["errors"]
    market, index = result["records"]
    assert market["values"]["ValueTraded"] == "1099.7500"
    assert market["values"]["Volume"] == 101
    assert market["values"]["ValueTraded"] != "1111.0000"  # close * quantity is not turnover
    assert market["source"]["source_priority"] == 2
    assert index["values"]["ClosePrice"] == "11.0000"
    assert result["summary"] == {"records": 2, "ready": 2}


def test_today_intraday_is_quarantined(tmp_path):
    with client_for([row("THYAO", "2026-09-22T16:30:00+03:00")]) as client:
        result = build_isyatirim_daily(company_map={"THYAO": 7}, company_codes=["THYAO"],
            archive_dir=tmp_path, pause_seconds=0, client=client,
            now=datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc))
    assert not result["records"]
    assert "intraday" in result["errors"][0]["error"]


def test_previous_trading_day_allowed_before_close(tmp_path):
    with client_for([row("THYAO", "2026-09-21T15:00:00+03:00")]) as client:
        result = build_isyatirim_daily(company_map={"THYAO": 7}, company_codes=["THYAO"],
            archive_dir=tmp_path, pause_seconds=0, client=client,
            now=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc))
    assert result["summary"]["ready"] == 1
    assert result["records"][0]["values"]["TradeDate"] == "2026-09-21"


def test_missing_symbol_is_reported(tmp_path):
    with client_for([row("THYAO")]) as client:
        result = build_isyatirim_daily(company_map={"THYAO": 7, "ASELS": 8},
            archive_dir=tmp_path, pause_seconds=0, client=client,
            now=datetime(2026, 9, 22, 19, 0, tzinfo=timezone.utc))
    assert len(result["records"]) == 1
    assert result["errors"] == [{"symbol": "ASELS", "error": "symbol_missing_from_provider_response"}]


def test_throttle_stops_run(tmp_path):
    with client_for([], status=429) as client:
        with pytest.raises(ValueError, match="throttled"):
            fetch_isyatirim_snapshots(["THYAO"], archive_dir=tmp_path,
                pause_seconds=0, client=client)


def test_provider_batch_limit_is_enforced(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 20"):
        fetch_isyatirim_snapshots(["THYAO"], archive_dir=tmp_path, batch_size=21)
