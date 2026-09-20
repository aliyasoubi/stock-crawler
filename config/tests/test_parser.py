from decimal import Decimal

import pytest

from stock_crawler.models import ParseStatus
from stock_crawler.parser import PARSER_VERSION, build_report, extract_facts_from_html, normalize_label, parse_snapshot

D = Decimal


def parse(html: str):
    return build_report(extract_facts_from_html(html.encode("utf-8")))


def test_normalize_label_is_turkish_aware():
    assert normalize_label("DÖNEM KARI (ZARARI)") == "dönem karı (zararı)"
    assert normalize_label("Geri Alınmış Paylar (-)") == "geri alınmış paylar"
    assert normalize_label("  Kâr  veya   Zarar ") == "kar veya zarar"
    assert normalize_label("İşletme") == "işletme"


def test_reference_fixture_baseline_and_additional_fields(thyao_html):
    report = parse(thyao_html)
    assert report.parse_status is ParseStatus.VALID, report.errors
    assert report.consolidation_scope.value == "consolidated"
    assert report.statement_type == "general"
    assert (report.filing_fiscal_year, str(report.filing_period_end_date)) == (2024, "2024-12-31")
    current = report.current_period()
    assert current.currency_code == "TRY" and current.currency_scale == 1000 and current.presentation_currency_raw == "1000TL"
    assert current.total_assets == D(1_000_000_000)
    assert current.total_liabilities_and_equity == D(1_000_000_000)
    assert current.current_liabilities == D(300_000_000)
    assert current.non_current_liabilities == D(400_000_000)
    assert current.total_equity == D(300_000_000)
    assert current.revenue == D(700_000_000)
    assert current.net_profit == D(110_000_000)
    assert current.profit_attributable_to_owners_of_parent == D(108_000_000)
    assert current.profit_attributable_to_non_controlling_interests == D(2_000_000)
    assert current.finance_sector_revenue is None
    assert current.eps == D("0.78")  # per-share unit, never scaled
    assert current.operating_income == D(150_000_000)
    assert current.cash_and_cash_equivalents == D(120_000_000)
    assert (current.total_debt, current.total_debt_method) == (D(430_000_000), "sum_borrowings")
    assert (current.ebitda, current.ebitda_method) == (None, "missing")
    assert (current.free_cash_flow, current.free_cash_flow_method) == (D(95_000_000), "operating_cf_minus_capex")
    assert (current.shares_outstanding, current.shares_outstanding_method) == (None, "missing")
    assert not any("identity check" in w for w in report.warnings)


def test_comparative_row_is_linked_with_its_own_dates(thyao_html):
    report = parse(thyao_html)
    assert [p.is_comparative for p in report.periods] == [False, True]
    comparative = report.periods[1]
    assert (comparative.fiscal_year, str(comparative.period_start_date), str(comparative.period_end_date)) == (2023, "2023-01-01", "2023-12-31")
    assert comparative.revenue == D(500_000_000) and comparative.total_debt == D(345_000_000)
    assert str(comparative.measuring_unit_date) == '2024-12-31'
    assert comparative.measuring_unit_date == report.current_period().measuring_unit_date


def test_field_sources_record_provenance(thyao_html):
    report = parse(thyao_html)
    source = report.field_sources["2024:current"]["eps"]
    assert source["label"] == "Pay Başına Kazanç" and source["unit"] == "per_share" and source["scaled_by"] == 1
    debt = report.derivations["2024:current"]["total_debt"]
    assert debt["inputs"]["long_term_borrowings"] == "350000000"


@pytest.mark.parametrize("raw, scale", [("TL", 1), ("1000TL", 1000), ("1000000TL", 1_000_000)])
def test_three_presentation_scales(thyao_html, raw, scale):
    report = parse(thyao_html.replace("<td>1000TL</td>", f"<td>{raw}</td>"))
    current = report.current_period()
    assert current.currency_scale == scale and current.revenue == D(700_000) * scale and current.eps == D("0.78")


def test_unknown_currency_fails_instead_of_assuming_try(thyao_html):
    report = parse(thyao_html.replace("<td>1000TL</td>", "<td>Bin Lira</td>"))
    assert report.parse_status is ParseStatus.FAILED and any("presentation currency" in e for e in report.errors)


def test_null_versus_zero(thyao_html):
    zero = parse(thyao_html.replace("<td>Nakit ve Nakit Benzerleri</td><td>120.000</td>", "<td>Nakit ve Nakit Benzerleri</td><td>0</td>"))
    assert zero.current_period().cash_and_cash_equivalents == D(0)
    dash = parse(thyao_html.replace("<td>Nakit ve Nakit Benzerleri</td><td>120.000</td>", "<td>Nakit ve Nakit Benzerleri</td><td>-</td>"))
    assert dash.current_period().cash_and_cash_equivalents is None
    assert dash.parse_status is ParseStatus.VALID


def test_missing_required_concept_is_a_failure_not_empty_data(thyao_html):
    report = parse(thyao_html.replace("<td>Hasılat</td>", "<td>Satış Gelirleri</td>"))
    assert report.parse_status is ParseStatus.FAILED and "revenue" in " ".join(report.errors)
    assert report.periods == []


def test_missing_statement_table_prevents_publication(thyao_html):
    start = thyao_html.index("<h3>Nakit Akış")
    without_cash_flow = thyao_html[:start] + "</body></html>"
    report = parse(without_cash_flow)
    assert report.parse_status is ParseStatus.VALID
    assert report.current_period().free_cash_flow is None and report.current_period().ebitda is None
    start = thyao_html.index("<h3>Kar veya Zarar")
    end = thyao_html.index("<h3>Nakit Akış")
    report = parse(thyao_html[:start] + thyao_html[end:])
    assert report.parse_status is ParseStatus.FAILED and any("income_statement" in e for e in report.errors)


def test_interim_period_is_unsupported(thyao_html):
    interim = thyao_html.replace("<td>Yıllık</td>", "<td>9 Aylık</td>")
    assert parse(interim).parse_status is ParseStatus.UNSUPPORTED
    nine_months = thyao_html.replace("01.01.2024 - 31.12.2024", "01.01.2024 - 30.09.2024").replace("31.12.2024", "30.09.2024")
    report = parse(nine_months)
    assert report.parse_status is ParseStatus.UNSUPPORTED and "twelve-month" in report.errors[0]


def test_unsupported_statement_family(thyao_html):
    report = parse(thyao_html.replace("<td>Genel</td>", "<td>Bankalar</td>"))
    assert report.parse_status is ParseStatus.UNSUPPORTED and report.statement_type == "banks"


def test_ambiguous_context_without_ancestry_is_rejected(thyao_html):
    flat = thyao_html.replace(' data-level="0"', "").replace(' data-level="1"', "").replace(' data-level="2"', "").replace(' data-level="3"', "")
    report = parse(flat)
    assert report.parse_status is ParseStatus.VALID
    current = report.current_period()
    # "Ana Ortaklık Payları" appears under both P&L and comprehensive income: ambiguous, left NULL.
    assert current.profit_attributable_to_owners_of_parent is None
    assert any("profit_attributable_to_owners_of_parent: ambiguous" in w for w in report.warnings)
    # Lease lines can't be placed relative to borrowing subtotals: total_debt stays NULL with a reason.
    assert current.total_debt is None and "ancestry" in report.derivations["2024:current"]["total_debt"]["reason"]


def test_diluted_eps_is_never_mixed_in(thyao_html):
    no_basic = thyao_html.replace("<tr data-level=\"1\"><td>Pay Başına Kazanç</td><td>0,78</td><td>0,39</td></tr>", "").replace(
        "<tr data-level=\"2\"><td>Sürdürülen Faaliyetlerden Pay Başına Kazanç</td><td>0,78</td><td>0,39</td></tr>", ""
    )
    assert parse(no_basic).current_period().eps is None


def test_standalone_lease_lines_are_added_once(thyao_html):
    html = thyao_html.replace(
        '<tr data-level="2"><td>Ticari Borçlar</td><td>220.000</td><td>185.000</td></tr>',
        '<tr data-level="2"><td>Kiralama İşlemlerinden Borçlar</td><td>7.000</td><td>6.000</td></tr>'
        '<tr data-level="2"><td>Ticari Borçlar</td><td>213.000</td><td>179.000</td></tr>',
    )
    current = parse(html).current_period()
    assert (current.total_debt, current.total_debt_method) == (D(437_000_000), "sum_borrowings_and_leases")


def test_identity_mismatch_is_a_warning_not_a_correction(thyao_html):
    report = parse(thyao_html.replace("<td>TOPLAM VARLIKLAR</td><td>1.000.000</td>", "<td>TOPLAM VARLIKLAR</td><td>1.000.900</td>"))
    assert report.parse_status is ParseStatus.VALID
    assert report.current_period().total_assets == D(1_000_900_000)
    assert any("identity check" in w for w in report.warnings)


def test_unparseable_value_becomes_warning(thyao_html):
    report = parse(thyao_html.replace("<td>Stoklar</td><td>150.000</td>", "<td>Stoklar</td><td>15O.OOO</td>"))
    assert report.parse_status is ParseStatus.VALID and any("Stoklar" in w for w in report.warnings)


def test_extra_balance_sheet_date_without_income_context_is_skipped(thyao_html):
    html = thyao_html.replace(
        "<th>Önceki Dönem<br>31.12.2023</th></tr>", "<th>Önceki Dönem<br>31.12.2023</th><th>Yeniden Düzenlenmiş<br>01.01.2023</th></tr>"
    )
    report = parse(html)
    assert report.parse_status is ParseStatus.VALID and len(report.periods) == 2
    assert any("2023-01-01" in w for w in report.warnings)


def test_parse_snapshot_checks_listing_scope(thyao_html):
    files = {"source.html": thyao_html.encode()}
    ok = parse_snapshot(files, {"primary_file": "source.html", "consolidation_scope": "consolidated"})
    assert ok.parse_status is ParseStatus.VALID and ok.parser_version == PARSER_VERSION
    mismatch = parse_snapshot(files, {"primary_file": "source.html", "consolidation_scope": "unconsolidated"})
    assert mismatch.parse_status is ParseStatus.FAILED and "scope mismatch" in mismatch.errors[-1]
    missing = parse_snapshot({"other.bin": b""}, {"primary_file": "source.html"})
    assert missing.parse_status is ParseStatus.FAILED


def test_english_labels_map_to_the_same_concepts():
    html = """
    <table><tr><td>Nature of Financial Statement</td><td>Consolidated</td></tr><tr><td>Presentation Currency</td><td>TL</td></tr>
    <tr><td>Period</td><td>Annual</td></tr></table>
    <h3>Statement of Financial Position</h3>
    <table><tr><th>Item</th><th>Current 31.12.2024</th></tr>
    <tr data-level="0"><td>Total Assets</td><td>100</td></tr>
    <tr data-level="0"><td>Current Liabilities</td><td>20</td></tr>
    <tr data-level="0"><td>Non-current Liabilities</td><td>30</td></tr>
    <tr data-level="0"><td>Total Equity</td><td>50</td></tr>
    <tr data-level="0"><td>Total Liabilities and Equity</td><td>100</td></tr></table>
    <h3>Statement of Profit or Loss</h3>
    <table><tr><th>Item</th><th>Current 01.01.2024 - 31.12.2024</th></tr>
    <tr><td>Revenue</td><td>80</td></tr><tr><td>Profit (Loss)</td><td>9</td></tr></table>
    """
    report = parse(html)
    assert report.parse_status is ParseStatus.VALID, report.errors
    assert report.current_period().revenue == D(80) and report.current_period().net_profit == D(9)


def test_listing_period_mismatch_blocks_publication(thyao_html):
    report = parse_snapshot({'source.html': thyao_html.encode()}, {'primary_file': 'source.html', 'fiscal_year': 2025, 'period_end_date': '2025-12-31'})
    assert report.parse_status is ParseStatus.FAILED
    assert 'fiscal_year mismatch' in ' '.join(report.errors)
