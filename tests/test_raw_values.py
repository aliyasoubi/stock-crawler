"""Every crawler copies values as the source publishes them: no scaling, rounding or recoding. Offline."""
from datetime import date
from decimal import Decimal
import io
import json
import zipfile

from stock_crawler.companies.parsers import parse_financial_currency
from stock_crawler.fundamentals import crawler as fundamentals
from stock_crawler.market_data.crawler import COLUMNS, iso_date, read_bulletin
from stock_crawler.market_index import crawler as market_index
from stock_crawler.market_index.export import write_data_csv
from stock_crawler.market_index.models import IndexInfo


def test_companies_currency_as_published():
    html = ("<table><tr><td>Sunum Para Birimi</td><td>TL</td><td>1000TL</td><td></td></tr></table>")
    assert parse_financial_currency(html) == "1000TL"  # latest non-empty, not recoded to TRY


def test_market_data_bulletin_values_copied():
    header = ["TRADE DATE", "INSTRUMENT SERIES CODE", "INSTRUMENT GROUP", *COLUMNS]
    values = {name: "1" for name in COLUMNS} | {"CLOSING PRICE": ".376", "CHANGE TO PREVIOUS CLOSING (%)": "-.5",
                                                 "TOTAL TRADED VOLUME": "100"}
    row = ["2016-01-04", "ABCDE.E", "EQT", *values.values()]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("thb.csv", "\n".join([";".join(header), ";".join(row)]))
    [parsed] = read_bulletin(buffer.getvalue())
    assert parsed[:2] == ["ABCDE", "2016-01-04"]
    assert parsed[2 + list(COLUMNS).index("CLOSING PRICE")] == ".376"
    assert parsed[2 + list(COLUMNS).index("CHANGE TO PREVIOUS CLOSING (%)")] == "-.5"


def test_market_data_bulletin_date_written_as_iso():
    """Some 2020 bulletins write 1.06.2020; SQL Server and the other files need 2020-06-01."""
    assert iso_date("1.06.2020") == "2020-06-01"
    assert iso_date("21.05.2020") == "2020-05-21"
    assert iso_date("2016-01-04") == "2016-01-04"


def test_fundamentals_values_not_scaled():
    assert fundamentals.parse_currency("1000TL") == ("1000TL", Decimal(1000))
    assert fundamentals.parse_number("1.166.684") == Decimal("1166684")  # KAP's thousands dots are read
    company = fundamentals.Company(oid="x", company_id=1, stock_code="ABC", title="ABC A.S.", sector="GENERAL")
    row = fundamentals.ExportRow(
        company_name="ABC A.S.", notification_id="1", publish_date=date(2025, 5, 1), year=2025, period=1,
        nature="Consolidated", currency="1000TL", multiplier=Decimal(1000), sectoral_type="general",
        values={fundamentals.ASSETS: Decimal("1166684"), fundamentals.EQUITY: Decimal("1.50")})
    out = fundamentals.to_table_row(company, row)
    assert out["TotalAssets"] == Decimal("1166684")
    assert out["PresentationCurrency"] == "1000TL"
    assert fundamentals.fmt(out["Equity"]) == "1.50"  # trailing zero kept


class FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, text):
        self.text, self.content = text, text.encode()

    def raise_for_status(self):
        pass

    def json(self, **kwargs):
        return json.loads(self.text, **kwargs)


class FakeSession:
    def get(self, url, params, timeout):
        # 2026-09-25 00:00 Istanbul; a close with float32 noise, as the source can send it
        return FakeResponse('{"data": [[1790283600000, 13892.2998046875]]}')


def test_market_index_close_copied_exactly(tmp_path):
    index = IndexInfo(index_id=1, code="XU100", market_id=1, name="BIST 100")
    [row] = market_index.fetch_index(FakeSession(), "https://x", index, date(2026, 9, 25), date(2026, 9, 25))
    assert row.close_price == Decimal("13892.2998046875")
    write_data_csv([row], tmp_path / "data.csv")
    assert (tmp_path / "data.csv").read_text().splitlines()[1] == "2026-09-25,1,13892.2998046875"
