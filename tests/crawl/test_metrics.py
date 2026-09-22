from decimal import Decimal

from stock_crawler.crawl.metrics import DebtComponents, ebitda, free_cash_flow, identity_checks, shares_outstanding, total_debt

D = Decimal


def test_total_debt_sums_non_overlapping_borrowings_without_leases_when_none_reported():
    result = total_debt(DebtComponents(short_term_borrowings=D(50), current_portion_of_long_term_borrowings=D(30), long_term_borrowings=D(350)))
    assert (result.value, result.method) == (D(430), "sum_borrowings")


def test_total_debt_adds_standalone_leases_exactly_once():
    result = total_debt(
        DebtComponents(
            short_term_borrowings=D(50), current_portion_of_long_term_borrowings=D(30), long_term_borrowings=D(350),
            short_term_lease_liabilities=D(10), long_term_lease_liabilities=D(200), lease_lines_reported=True,
        )
    )
    assert (result.value, result.method) == (D(640), "sum_borrowings_and_leases")


def test_total_debt_direct_total_is_not_double_counted():
    result = total_debt(DebtComponents(borrowings_total=D(430), borrowings_total_includes_leases=True, short_term_lease_liabilities=D(10), lease_lines_reported=True))
    assert (result.value, result.method) == (D(430), "direct")


def test_total_debt_missing_component_is_null_with_reason():
    result = total_debt(DebtComponents(short_term_borrowings=D(50)))
    assert result.value is None and result.method == "missing" and "long-term" in result.reason
    assert total_debt(DebtComponents()).reason == "no borrowing lines identified"
    partial_lease = total_debt(DebtComponents(short_term_borrowings=D(1), long_term_borrowings=D(1), short_term_lease_liabilities=D(1), lease_lines_reported=True))
    assert partial_lease.value is None


def test_ebitda_prefers_reported_then_operating_income_plus_da():
    assert ebitda(D(999), D(150), D(60)).method == "direct"
    derived = ebitda(None, D(150), D(-60))
    assert (derived.value, derived.method) == (D(210), "operating_income_plus_da")
    assert ebitda(None, D(150), None).value is None
    assert ebitda(None, None, D(60)).value is None
    assert ebitda(None, D(150), D(60), da_deducted_in_operating_income=False).value is None


def test_free_cash_flow_normalises_outflow_signs_and_never_defaults_capex_to_zero():
    assert free_cash_flow(D(100), D(-20), D(-5)).value == D(75)
    assert free_cash_flow(D(100), D(20), D(5)).value == D(75)
    combined = free_cash_flow(D(100), None, None, combined_ppe_and_intangible_purchases=D(-25))
    assert combined.value == D(75) and combined.inputs["capex"] == D(25)
    assert free_cash_flow(D(100), D(-20), None).value is None
    assert free_cash_flow(None, D(-20), D(-5)).value is None


def test_shares_outstanding_requires_known_treasury_count():
    assert shares_outstanding(D(1380), None, None, None, treasury_shares_known=False).method == "direct"
    derived = shares_outstanding(None, D(1380), D(1), D(10), treasury_shares_known=True)
    assert (derived.value, derived.method) == (D(1370), "capital_less_treasury")
    unknown = shares_outstanding(None, D(1380), D(1), None, treasury_shares_known=False)
    assert unknown.value is None and "treasury" in unknown.reason
    assert shares_outstanding(None, D(1380), D(0), D(0), treasury_shares_known=True).value is None


def test_identity_checks_are_diagnostics_with_tolerance():
    ok = {"total_assets": D(1000), "total_liabilities_and_equity": D(1000), "current_liabilities": D(300), "non_current_liabilities": D(400), "total_equity": D(300), "net_profit": D(110), "profit_attributable_to_owners_of_parent": D(108), "profit_attributable_to_non_controlling_interests": D(2)}
    assert identity_checks(ok, tolerance=D(3)) == []
    bad = {**ok, "total_assets": D(1010)}
    findings = identity_checks(bad, tolerance=D(3))
    assert len(findings) == 1 and "total_assets" in findings[0]
    assert identity_checks({"total_assets": D(1)}, tolerance=D(0)) == []
