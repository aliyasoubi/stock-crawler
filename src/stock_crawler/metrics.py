"""Pure derived-metric calculations with explicit methods and reasons (README section 10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class MetricResult:
    value: Decimal | None
    method: str
    reason: str | None = None
    inputs: dict[str, Decimal | None] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "value": None if self.value is None else str(self.value),
            "method": self.method,
            "reason": self.reason,
            "inputs": {key: (None if val is None else str(val)) for key, val in self.inputs.items()},
        }


@dataclass(frozen=True)
class DebtComponents:
    """Non-overlapping borrowing lines. `borrowings_total` is a source-reported total that
    already contains every other component; `borrowings_total_includes_leases` says whether
    lease liabilities are inside that subtotal."""

    borrowings_total: Decimal | None = None
    borrowings_total_includes_leases: bool = False
    short_term_borrowings: Decimal | None = None
    current_portion_of_long_term_borrowings: Decimal | None = None
    long_term_borrowings: Decimal | None = None
    short_term_lease_liabilities: Decimal | None = None
    long_term_lease_liabilities: Decimal | None = None
    lease_lines_reported: bool = False


def total_debt(components: DebtComponents) -> MetricResult:
    """Direct verified total, else sum non-overlapping borrowings plus leases exactly once."""
    inputs = {
        "borrowings_total": components.borrowings_total,
        "short_term_borrowings": components.short_term_borrowings,
        "current_portion_of_long_term_borrowings": components.current_portion_of_long_term_borrowings,
        "long_term_borrowings": components.long_term_borrowings,
        "short_term_lease_liabilities": components.short_term_lease_liabilities,
        "long_term_lease_liabilities": components.long_term_lease_liabilities,
    }
    leases = [components.short_term_lease_liabilities, components.long_term_lease_liabilities]
    lease_sum = sum((item for item in leases if item is not None), Decimal(0))
    if components.borrowings_total is not None:
        if components.borrowings_total_includes_leases or not components.lease_lines_reported:
            return MetricResult(components.borrowings_total, "direct", None, inputs)
        return MetricResult(components.borrowings_total + lease_sum, "direct_plus_leases", None, inputs)

    borrowings = [
        components.short_term_borrowings,
        components.current_portion_of_long_term_borrowings,
        components.long_term_borrowings,
    ]
    if all(item is None for item in borrowings):
        return MetricResult(None, "missing", "no borrowing lines identified", inputs)
    if components.long_term_borrowings is None or components.short_term_borrowings is None:
        return MetricResult(None, "missing", "short-term or long-term borrowings line not identified", inputs)
    total = sum((item for item in borrowings if item is not None), Decimal(0))
    if components.lease_lines_reported:
        if any(item is None for item in leases):
            return MetricResult(None, "missing", "lease liabilities reported but one maturity bucket is unresolved", inputs)
        return MetricResult(total + lease_sum, "sum_borrowings_and_leases", None, inputs)
    return MetricResult(total, "sum_borrowings", None, inputs)


def ebitda(
    reported_ebitda: Decimal | None,
    operating_income: Decimal | None,
    depreciation_and_amortisation: Decimal | None,
    *,
    da_deducted_in_operating_income: bool = True,
) -> MetricResult:
    inputs = {
        "reported_ebitda": reported_ebitda,
        "operating_income": operating_income,
        "depreciation_and_amortisation": depreciation_and_amortisation,
    }
    if reported_ebitda is not None:
        return MetricResult(reported_ebitda, "direct", None, inputs)
    if operating_income is None:
        return MetricResult(None, "missing", "operating income unavailable", inputs)
    if depreciation_and_amortisation is None:
        return MetricResult(None, "missing", "depreciation and amortisation unavailable", inputs)
    if not da_deducted_in_operating_income:
        return MetricResult(None, "missing", "D&A not demonstrably deducted in operating income", inputs)
    return MetricResult(operating_income + abs(depreciation_and_amortisation), "operating_income_plus_da", None, inputs)


def free_cash_flow(
    operating_cash_flow: Decimal | None,
    ppe_purchases: Decimal | None,
    intangible_purchases: Decimal | None,
    combined_ppe_and_intangible_purchases: Decimal | None = None,
) -> MetricResult:
    """Operating cash flow minus cash CapEx. Outflows may be reported negative; CapEx is
    normalized to a positive magnitude. Missing components never default to zero."""
    inputs = {
        "operating_cash_flow": operating_cash_flow,
        "ppe_purchases": ppe_purchases,
        "intangible_purchases": intangible_purchases,
        "combined_ppe_and_intangible_purchases": combined_ppe_and_intangible_purchases,
    }
    if operating_cash_flow is None:
        return MetricResult(None, "missing", "operating cash flow unavailable", inputs)
    if combined_ppe_and_intangible_purchases is not None:
        capex = abs(combined_ppe_and_intangible_purchases)
    elif ppe_purchases is not None and intangible_purchases is not None:
        capex = abs(ppe_purchases) + abs(intangible_purchases)
    else:
        return MetricResult(None, "missing", "PPE or intangible purchase line unavailable", inputs)
    return MetricResult(operating_cash_flow - capex, "operating_cf_minus_capex", None, {**inputs, "capex": capex})


def shares_outstanding(
    direct_outstanding: Decimal | None,
    issued_capital: Decimal | None,
    nominal_value_per_share: Decimal | None,
    treasury_shares_count: Decimal | None,
    *,
    treasury_shares_known: bool,
) -> MetricResult:
    inputs = {
        "direct_outstanding": direct_outstanding,
        "issued_capital": issued_capital,
        "nominal_value_per_share": nominal_value_per_share,
        "treasury_shares_count": treasury_shares_count,
    }
    if direct_outstanding is not None:
        return MetricResult(direct_outstanding, "direct", None, inputs)
    if issued_capital is None or nominal_value_per_share in (None, Decimal(0)):
        return MetricResult(None, "missing", "issued capital or nominal value per share unavailable", inputs)
    if not treasury_shares_known:
        return MetricResult(None, "missing", "treasury share count unknown; not assumed zero", inputs)
    issued = issued_capital / nominal_value_per_share
    return MetricResult(issued - (treasury_shares_count or Decimal(0)), "capital_less_treasury", None, inputs)


def identity_checks(values: dict[str, Decimal | None], *, tolerance: Decimal) -> list[str]:
    """Accounting identities as diagnostics; never used to alter reported values."""
    findings: list[str] = []

    def check(label: str, left: Decimal | None, right: Decimal | None) -> None:
        if left is None or right is None:
            return
        if abs(left - right) > tolerance:
            findings.append(f"{label}: {left} vs {right} (diff {left - right})")

    check("total_assets != total_liabilities_and_equity", values.get("total_assets"), values.get("total_liabilities_and_equity"))
    parts = [values.get("current_liabilities"), values.get("non_current_liabilities"), values.get("total_equity")]
    if all(part is not None for part in parts):
        check("liabilities + equity != total_liabilities_and_equity", sum(parts, Decimal(0)), values.get("total_liabilities_and_equity"))
    owners, nci = values.get("profit_attributable_to_owners_of_parent"), values.get("profit_attributable_to_non_controlling_interests")
    if owners is not None and nci is not None:
        check("owners + non-controlling != net_profit", owners + nci, values.get("net_profit"))
    return findings
