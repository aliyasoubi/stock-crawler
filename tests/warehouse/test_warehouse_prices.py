from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import httpx
import pytest

from stock_crawler.warehouse.prices import (build_isyatirim_daily, build_isyatirim_history,
                                            build_listing_status, fetch_isyatirim_snapshots)


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


# -- historical backfill ---------------------------------------------------------------

# Field values captured from the live HisseTekil response for ASELS on 2015-01-02. The two
# series diverge by 8.45x at this date, which is what makes it a usable regression fixture:
# HG_* is the traded series (HG_KAPANIS 12.00 == PD / SERMAYE), HGDG_* is back-adjusted.
ASELS_2015 = {
    "HGDG_HS_KODU": "ASELS", "HGDG_TARIH": "02-01-2015",
    "HGDG_KAPANIS": Decimal("1.42"), "HG_KAPANIS": Decimal("12.0"),
    "HGDG_MIN": Decimal("1.4022015"), "HG_MIN": Decimal("11.9"),
    "HGDG_MAX": Decimal("1.4317838"), "HG_MAX": Decimal("12.1"),
    "HGDG_AOF": Decimal("1.4177461"), "HG_AOF": Decimal("11.99"),
    "HGDG_HACIM": Decimal("6402482.0"), "HG_HACIM": Decimal("4563133.0"),
    "SERMAYE": Decimal("500000000.0"), "PD": Decimal("6000000000.0"),
}


class FakeFetcher:
    """Stands in for PacedClient: the backfill only needs `.get(url).content`."""

    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, *, accept=None, validators=None):
        self.urls.append(url)
        body = json.dumps(self.payload, default=str).encode()
        return SimpleNamespace(url=url, status=200, content=body,
                               content_type="application/json", not_modified=False)


def history(rows, ok=True, **extra):
    return {"ok": ok, "errorCode": None, "errorDescription": None,
            "transactionId": "t", "value": rows, **extra}


def build(rows, tmp_path, **kwargs):
    fetcher = FakeFetcher(history(rows))
    result = build_isyatirim_history("ASELS", 4, start=date(2015, 1, 1), end=date(2015, 1, 31),
                                     fetcher=fetcher, archive_dir=tmp_path, **kwargs)
    return result, fetcher


def test_history_maps_raw_series_never_the_adjusted_one(tmp_path):
    result, fetcher = build([ASELS_2015], tmp_path)
    assert not result["errors"]
    values = result["records"][0]["values"]
    # Raw traded close, not the 1.42 back-adjusted figure.
    assert values["ClosePrice"] == "12.0000"
    assert values["HighPrice"] == "12.1000"
    assert values["LowPrice"] == "11.9000"
    assert values["ValueTraded"] == "4563133.0000"
    # 4563133 / 11.99; using the adjusted pair would yield 4515958 instead.
    assert values["Volume"] == 380578
    assert values["TradeDate"] == "2015-01-02"
    assert values["CompanyId"] == 4
    assert "hisse=ASELS" in fetcher.urls[0] and "startdate=01-01-2015" in fetcher.urls[0]


def test_history_row_loads_with_null_open_price(tmp_path):
    result, _ = build([ASELS_2015], tmp_path)
    record = result["records"][0]
    assert record["values"]["OpenPrice"] is None
    assert record["missing_required_fields"] == []
    assert record["validation_issues"] == []
    assert result["summary"] == {"records": 1, "ready": 1}
    assert record["source"]["price_basis"] == "as_traded"
    assert record["source"]["derivations"]["Volume"]["method"] == "turnover_divided_by_vwap"


def test_zero_vwap_is_reported_rather_than_guessing_volume(tmp_path):
    result, _ = build([dict(ASELS_2015, HG_AOF=Decimal("0"))], tmp_path)
    assert result["records"] == []
    assert "share volume cannot be derived" in result["errors"][0]["error"]
    assert result["errors"][0]["trade_date"] == "2015-01-02"


def test_foreign_symbol_and_out_of_range_rows_are_rejected(tmp_path):
    result, _ = build([dict(ASELS_2015, HGDG_HS_KODU="THYAO"),
                       dict(ASELS_2015, HGDG_TARIH="02-01-2020")], tmp_path)
    assert result["records"] == []
    reasons = " ".join(error["error"] for error in result["errors"])
    assert "requested 'ASELS'" in reasons and "outside the requested date range" in reasons


def test_duplicate_trade_date_is_rejected(tmp_path):
    result, _ = build([ASELS_2015, ASELS_2015], tmp_path)
    assert len(result["records"]) == 1
    assert "duplicate trade date" in result["errors"][0]["error"]


def test_provider_error_payload_stops_the_symbol(tmp_path):
    fetcher = FakeFetcher(history([], ok=False, errorCode="X1", errorDescription="nope"))
    with pytest.raises(ValueError, match="rejected the history request"):
        build_isyatirim_history("ASELS", 4, start=date(2015, 1, 1), end=date(2015, 1, 31),
                                fetcher=fetcher, archive_dir=tmp_path)


# -- listing status --------------------------------------------------------------------

NAMES = {"THYAO": "TÜRK HAVA YOLLARI A.O.", "ARSNF": "ARSAN FİNANS FAKTORİNG A.Ş."}


def test_quoted_ticker_is_marked_active(tmp_path):
    with client_for([row("THYAO")]) as client:
        result = build_listing_status(company_map={"THYAO": 7}, names=NAMES, market_id=1,
                                      archive_dir=tmp_path, pause_seconds=0, client=client)
    record = result["records"][0]
    assert record["values"] == {"CompanyId": 7, "Ticker": "THYAO", "MarketId": 1,
                                "FullName": "TÜRK HAVA YOLLARI A.O.", "IsActive": 1}
    assert record["missing_required_fields"] == [] and record["validation_issues"] == []
    assert result["summary"] == {"records": 1, "ready": 1}


def test_unquoted_registrant_is_not_written_as_inactive(tmp_path):
    """A bond/factoring issuer that never had listed stock must not be asserted delisted."""
    with client_for([row("THYAO")]) as client:
        result = build_listing_status(company_map={"THYAO": 7, "ARSNF": 8}, names=NAMES,
                                      market_id=1, archive_dir=tmp_path, pause_seconds=0,
                                      client=client)
    assert [r["values"]["Ticker"] for r in result["records"]] == ["THYAO"]
    assert result["listing_survey"]["unquoted_tickers"] == ["ARSNF"]
    assert result["listing_survey"]["quoted"] == 1
    # An unquoted company is a review item, not a load error: errors would block --apply.
    assert result["errors"] == []


def test_quote_without_a_last_price_is_not_evidence(tmp_path):
    quote = row("THYAO")
    quote["last"] = None
    with client_for([quote]) as client:
        result = build_listing_status(company_map={"THYAO": 7}, names=NAMES, market_id=1,
                                      archive_dir=tmp_path, pause_seconds=0, client=client)
    assert result["records"] == []
    assert result["listing_survey"]["unquoted_tickers"] == ["THYAO"]


def test_empty_company_map_is_refused(tmp_path):
    with pytest.raises(ValueError, match="seed dbo.Company"):
        build_listing_status(company_map={}, names={}, market_id=1, archive_dir=tmp_path)


def test_history_response_is_archived_by_hash(tmp_path):
    build([ASELS_2015], tmp_path)
    archived = list((tmp_path / "raw" / "isyatirim_public_history").glob("*/source.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["value"][0]["HG_KAPANIS"] == "12.0"
