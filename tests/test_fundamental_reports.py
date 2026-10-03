"""fundamental_reports: reading KAP's full-report export and mapping lines to columns. Offline."""
import csv
from decimal import Decimal

from stock_crawler.fundamental_reports import crawler as reports


def line(template, role, row, label, *values, typed=False):
    """One report line as KAP writes it (values as raw title attributes)."""
    cls = f"{'typed-dimension-row with-plus-row ' if typed else ''}{template}_role_{role}-row-{row} data-input-row"
    cells = "".join(f'<td class="taxonomy-context-value col-order-class-{i}"><div>'
                    f'<div class="gwt-Label taxonomy-label-field" title="{v}">{v}</div></div></td>'
                    for i, v in enumerate(values))
    return (f'<tr class="{cls}"><td class="taxonomy-field-title"><table><tbody><tr><td>'
            f'<div class="gwt-Label multi-language-content content-en{" typed-dimension-field-label" if typed else ""}"'
            f' style="display: block;">\n {label}\n </div></td></tr></tbody></table></td>'
            f'<td class="taxonomy-footnote-cell"><div><div class="taxonomy-footnote-value">16</div></div></td>'
            f'{cells}</tr>')


def share_class(caption, value):
    return (f'<tr class="new-type-row"><td></td><td></td><td class="bordered-cell"><div>'
            f'<div class="taxonomy-label-field typed-dimension-field-caption"> {caption} </div></div></td>'
            f'<td class="taxonomy-footnote-cell"><div><div class="taxonomy-footnote-value"> 32 </div></div></td>'
            f'<td class="taxonomy-context-value"><div><div class="taxonomy-label-field"> {value} </div></div></td></tr>')


GENERAL = (
    '<table class="financial-header-table"><tbody><tr><td class="financial-header-title">Presentation Currency</td>'
    '<td>1.000 TL</td></tr></tbody></table><table class="financial-table tbl_general_role_210015"><tbody>'
    '<tr class="general_role_210015-row-1 abstract-row"><td>Assets</td></tr>'
    + line("general", "210015", 2, "Cash and cash equivalents", "1152672", "6425793")
    + line("general", "210015", 3, "Current Borrowings", "1000", "900")
    + line("general", "210015", 4, "Current Portion of Non-current Borrowings", "-0", "")
    + line("general", "210015", 5, "Long Term Borrowings", "500", "400")
    + line("general", "210015", 6, "Issued capital", "400000", "400000")
    + line("general", "210500", 1, "Cash and cash equivalents", "999", "")  # off-balance sheet: ignored
    + line("general", "310003", 1, "PROFIT (LOSS) FROM OPERATING ACTIVITIES", "-112", "-98", "-31", "-56")
    + line("general", "310003", 2, "Basic Earnings (Loss) Per Share from Continuing Operations", "", "", typed=True)
    + share_class("Sürdürülen Faaliyetlerden Pay Başına Kazanç (Zarar)", "2,19000000")
    + line("general", "520003", 1, "CASH FLOWS FROM (USED IN) OPERATING ACTIVITIES", "-144", "242")
    + line("general", "520003", 2, "Adjustments for depreciation and amortisation expense", "4", "8")
    + line("general", "520003", 3, "Purchase of property, plant and equipment", "-2", "-5")
    + line("general", "520003", 4, "Purchase of intangible assets", "-1", "")
    + "</tbody></table>"
)


def test_parse_report_reads_current_period_and_share_classes():
    report = reports.parse_report(GENERAL)
    assert report.template == "general" and report.currency == "1000TL"
    assert ("210015", "Cash and cash equivalents", "1152672") in report.lines
    assert ("310003", "Basic Earnings (Loss) Per Share from Continuing Operations | "
                      "Sürdürülen Faaliyetlerden Pay Başına Kazanç (Zarar)", "2,19000000") in report.lines


def test_general_columns():
    values, used = reports.columns_from(reports.parse_report(GENERAL), net_income=Decimal(-85000))
    assert values == {
        "CashAndEquivalents": Decimal(1152672),                # balance sheet, not off-balance
        "TotalDebtShort": Decimal(1000),                        # -0 counts as 0
        "TotalDebtLong": Decimal(500),
        "Ebitda": Decimal(-108),                                # -112 + 4
        "FreeCashFlow": Decimal(-147),                          # -144 - 2 - 1
        "Eps": Decimal("-0.21250000"),                          # -85,000 x 1000 TL / 400,000,000 shares
        "SharesOutstanding": Decimal(400000000),                # 400000 x 1000 TL / 1 TL nominal
    }
    assert ("Ebitda", "cash flow", "Adjustments for depreciation and amortisation expense", Decimal(4)) in used
    assert ("Eps", "fundamentals", "NetIncome", Decimal(-85000)) in used


def test_unknown_template_and_foreign_currency():
    assert reports.columns_from(reports.Report("pension", "TL", [("210001", "Cash and cash equivalents", "5")])) \
        == ({}, [])
    values, _ = reports.columns_from(reports.Report("general", "USD", [("210015", "Issued capital", "5")]))
    assert "SharesOutstanding" not in values  # capital in USD is not a share count


def test_chosen_notification_matches_fundamentals(tmp_path):
    raw = tmp_path / "raw.csv"
    rows = [("1", "2026", "2", "2026-08-01", "10", "Unconsolidated"),
            ("1", "2026", "2", "2026-07-01", "11", "Consolidated"),
            ("1", "2026", "2", "2026-07-02", "12", "Consolidated")]
    with raw.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["CompanyId", "StockCode", "FiscalYear", "FiscalQuarter", "PublishDate", "NotificationId",
                    "StatementNature"])
        w.writerows((c, "ABC", y, q, d, n, nature) for c, y, q, d, n, nature in rows)
    assert reports.chosen_notifications(raw)[(1, 2026, 2)]["NotificationId"] == "12"


def test_market_zip_fills_the_cache(tmp_path):
    """KAP's whole-quarter zip: every CODE_NotificationId_Year_Period.xls entry is cached by id."""
    import zipfile
    from stock_crawler.http_client import HttpClient
    source = tmp_path / "2026-2.zip"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr("KUYAS_1665630_2026_2.xls", GENERAL)
        z.writestr("NRBNK-NYB_1647428_2026_2.xls", GENERAL)
        z.writestr("readme.txt", "not a report")
    cache = reports.ReportCache(HttpClient(interval=0.5), tmp_path / "cache", reports.REPORT_URL)
    url = (tmp_path / "{year}-{quarter}.zip").as_uri().replace("%7B", "{").replace("%7D", "}")
    assert reports.download_market(cache, url, 2026, 2, proxy=None) == 2
    assert cache.load("1665630").currency == "1000TL" and cache.path("1647428").exists()
    assert not (tmp_path / "cache" / "market" / "2026-Q2.zip").exists()  # deleted once read
    assert reports.download_market(cache, url, 2025, 4, proxy=None) == 0  # no such file: falls back
