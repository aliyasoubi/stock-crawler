from decimal import Decimal

from stock_crawler.compare import classify, compare_reports, compare_rows, render_table


def report(**over):
    base = {"company_id": 1, "notification_id": "100", "content_hash": "h1", "parser_version": "1.0.0", "filing_fiscal_year": 2024, "consolidation_scope": "consolidated"}
    return {**base, **over}


def row(**over):
    base = {"fiscal_year": 2024, "fiscal_period": 4, "is_comparative": False, "currency_code": "TRY", "revenue": Decimal(700), "net_profit": Decimal(110), "eps": None, "total_debt_method": "sum_borrowings"}
    return {**base, **over}


def test_classification():
    assert classify(report(), report(content_hash="h2")) == "source_content_change"
    assert classify(report(), report(parser_version="1.1.0")) == "parser_change"
    assert classify(report(), report()) == "identical_version"
    assert classify(report(), report(notification_id="101")) == "new_publication_or_correction"
    assert classify(report(), report(notification_id="101", filing_fiscal_year=2025)) == "new_period"
    assert classify(report(), report(notification_id="101", consolidation_scope="unconsolidated")) == "different_scope"
    assert classify(report(), report(company_id=2)) == "different_company"


def test_row_diffs_with_null_transitions_and_deltas():
    diffs = compare_rows([row()], [row(revenue=Decimal(710), eps=Decimal("0.5"), net_profit=None, total_debt_method="missing")])
    assert diffs[0].status == "matched"
    by_field = {d.field: d for d in diffs[0].diffs}
    assert by_field["revenue"].delta == Decimal(10) and by_field["revenue"].kind == "changed"
    assert by_field["eps"].kind == "became_value" and by_field["eps"].delta is None
    assert by_field["net_profit"].kind == "became_null"
    assert by_field["total_debt_method"].after == "missing"


def test_incompatible_currency_gives_no_numeric_delta_and_unmatched_rows_are_listed():
    diffs = compare_rows([row()], [row(revenue=Decimal(710), currency_code="USD"), row(fiscal_year=2023, is_comparative=True)])
    matched = next(d for d in diffs if d.status == "matched")
    assert next(d for d in matched.diffs if d.field == "revenue").delta is None
    assert [d.status for d in diffs] == ["only_after", "matched"]


def test_compare_reports_notes_and_render():
    comparison = compare_reports(report(), [row()], report(notification_id="101", consolidation_scope="unconsolidated"), [row(revenue=Decimal(1))])
    assert comparison.classification == "different_scope" and comparison.notes
    assert comparison.rows[0].diffs[0].delta is None
    text = render_table(comparison)
    assert "different_scope" in text and "revenue" in text
    assert comparison.changed_fields == ["revenue"]
