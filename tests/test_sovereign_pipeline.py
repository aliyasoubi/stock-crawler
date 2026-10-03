import json
from datetime import date
from decimal import Decimal as D

import pytest

from stock_crawler.sovereign.fields import Agg, FieldSpec, Freq, PeriodType, SeriesSpec
from stock_crawler.sovereign.evds_client import EvdsClient, EvdsError, SeriesNotFoundError
from stock_crawler.sovereign.pipeline import build_rows, check_fits_column
from stock_crawler.sovereign.sources import CsvSource, EvdsSource
from stock_crawler.sovereign.writers import write_csv


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Routes requests to canned payloads and records them."""

    def __init__(self, catalog, data):
        self.catalog, self.data, self.calls = catalog, data, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if "/serieList/" in url:
            return FakeResponse(self.catalog[url.rsplit("code=", 1)[1]])
        code = kwargs["json"]["series"]
        if code not in self.data:
            return FakeResponse("<html>Error</html>", status=500)
        return FakeResponse(self.data[code])


def fe_payload(code, rows):
    key = code.replace(".", "_")
    return {"totalCount": len(rows), "items": [{"Tarih": t, key: v} for t, v in rows]}


def client_for(catalog, data):
    return EvdsClient(session=FakeSession(catalog, data), min_interval=0)


def test_fetch_series_parses_and_skips_nulls():
    data = {"TP.X": fe_payload("TP.X", [("2025-01", "1.5"), ("2025-02", None), ("2025-03", "2.25")])}
    obs = client_for({}, data).fetch_series("TP.X", Freq.MONTHLY, date(2025, 1, 1), date(2025, 3, 31))
    assert obs == {date(2025, 1, 1): D("1.5"), date(2025, 3, 1): D("2.25")}


def test_fetch_series_detects_truncation():
    payload = fe_payload("TP.X", [("2025-01", "1")])
    payload["totalCount"] = 5
    with pytest.raises(EvdsError, match="truncated"):
        client_for({}, {"TP.X": payload}).fetch_series("TP.X", Freq.MONTHLY, date(2025, 1, 1), date(2025, 1, 31))


def test_daily_series_fetched_in_yearly_chunks():
    session = FakeSession({}, {"TP.D": {"totalCount": 0, "items": []}})
    EvdsClient(session=session, min_interval=0).fetch_series("TP.D", Freq.BUSINESS_DAY, date(2023, 3, 1), date(2025, 2, 1))
    windows = [(c[2]["startDate"], c[2]["endDate"]) for c in session.calls]
    assert windows == [("01-03-2023", "31-12-2023"), ("01-01-2024", "31-12-2024"), ("01-01-2025", "01-02-2025")]


def test_http_error_raises():
    with pytest.raises(EvdsError, match="HTTP 500"):
        client_for({}, {}).fetch_series("TP.MISSING", Freq.MONTHLY, date(2025, 1, 1), date(2025, 1, 31))


def test_validate_fails_fast_on_unknown_code():
    source = EvdsSource(client_for({"bie_g": [{"SERIE_CODE": "TP.A"}]}, {}))
    with pytest.raises(SeriesNotFoundError, match="TP.B"):
        source.validate([SeriesSpec("TP.A", Freq.MONTHLY, Agg.SUM, "bie_g"), SeriesSpec("TP.B", Freq.MONTHLY, Agg.SUM, "bie_g")])


def test_check_fits_column_keeps_value_and_guards_overflow():
    spec = FieldSpec("X", (), precision=8, scale=4, unit="")
    assert str(check_fits_column(D("12.345678"), spec)) == "12.345678"  # not rounded
    with pytest.raises(OverflowError):
        check_fits_column(D("10000"), spec)


def test_build_rows_end_to_end(tmp_path):
    fields = (
        FieldSpec("Gdp", (SeriesSpec("TP.GDP", Freq.QUARTERLY, Agg.SUM, "g"),), 24, 4, "TRY"),
        FieldSpec("PublicDebt", (
            SeriesSpec("TP.D1", Freq.QUARTERLY, Agg.LAST, "g"),
            SeriesSpec("TP.D2", Freq.QUARTERLY, Agg.LAST, "g"),
        ), 24, 4, "TRY"),
        FieldSpec("Cpi", (SeriesSpec("TP.CPI", Freq.MONTHLY, Agg.AVG, "g"),), 10, 4, "idx",
                  valid_range=(D(0), None)),
        FieldSpec("CdsSpreadBps", (SeriesSpec("CDS", Freq.DAILY, Agg.LAST, source="cds"),), 10, 2, "bps"),
    )
    catalog = {"g": [{"SERIE_CODE": c} for c in ("TP.GDP", "TP.D1", "TP.D2", "TP.CPI")]}
    q = [("2025-Q1", "1"), ("2025-Q2", "2"), ("2025-Q3", "3"), ("2025-Q4", "4")]
    data = {
        "TP.GDP": fe_payload("TP.GDP", q),
        "TP.D1": fe_payload("TP.D1", q),
        "TP.D2": fe_payload("TP.D2", q[:3]),  # Q4 not yet published
        "TP.CPI": fe_payload("TP.CPI", [(f"2025-{m:02d}", "-5" if m <= 3 else "10") for m in range(1, 13)]),
    }
    cds_csv = tmp_path / "cds.csv"
    cds_csv.write_text("date,value\n2025-12-30,250.5\n2025-12-31,251.25\n")

    rows = build_rows(
        {"evds": EvdsSource(client_for(catalog, data)), "cds": CsvSource(cds_csv)},
        market_id=90, period_types=[PeriodType.QUARTERLY, PeriodType.ANNUAL],
        start=date(2025, 1, 1), end=date(2026, 1, 15), publish_date=date(2026, 1, 15), fields=fields,
    )
    by_key = {(r.PeriodType, r.AsOfDate): r.values for r in rows}

    annual = by_key[("ANNUAL", date(2025, 12, 31))]
    assert str(annual["Gdp"]) == "10"            # sum of the four quarters, in the source's unit
    assert "PublicDebt" not in annual            # a component is missing for Q4 -> no total
    assert annual["CdsSpreadBps"] == D("251.25")
    assert str(by_key[("QUARTERLY", date(2025, 9, 30))]["PublicDebt"]) == "6"
    assert "Cpi" not in by_key[("QUARTERLY", date(2025, 3, 31))]  # Q1 average -5 is out of range -> dropped
    assert annual["Cpi"] == D("6.2500")

    out = write_csv(rows, tmp_path / "out.csv")
    header, first = out.read_text().splitlines()[:2]
    assert header.startswith("MarketId,AsOfDate,PeriodType,PublishDate,Gdp,")
    assert first.startswith("90,2025-03-31,QUARTERLY,2026-01-15,1,")
