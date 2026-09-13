"""Typed records shared by the source client, parser, storage, and database layers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MARKET_SOURCE_KAP = "kap"
ANNUAL_PERIOD = 4  # legacy KAP contract: period 4 == twelve-month annual statement

BASELINE_FIELDS: tuple[str, ...] = (
    "total_liabilities_and_equity",
    "profit_attributable_to_non_controlling_interests",
    "profit_attributable_to_owners_of_parent",
    "current_liabilities",
    "non_current_liabilities",
    "total_equity",
    "total_assets",
    "revenue",
    "net_profit",
    "finance_sector_revenue",
)
ADDITIONAL_FIELDS: tuple[str, ...] = (
    "eps",
    "operating_income",
    "cash_and_cash_equivalents",
    "total_debt",
    "ebitda",
    "free_cash_flow",
    "shares_outstanding",
)
FINANCIAL_FIELDS: tuple[str, ...] = BASELINE_FIELDS + ADDITIONAL_FIELDS
METHOD_FIELDS: tuple[str, ...] = (
    "total_debt_method",
    "ebitda_method",
    "free_cash_flow_method",
    "shares_outstanding_method",
)
# Monetary fields are normalized by currency_scale; these two carry their own units.
NON_MONETARY_FIELDS: frozenset[str] = frozenset({"eps", "shares_outstanding"})


class ConsolidationScope(str, Enum):
    CONSOLIDATED = "consolidated"
    UNCONSOLIDATED = "unconsolidated"


class ParseStatus(str, Enum):
    VALID = "valid"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class CompanyIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    market_source: str = MARKET_SOURCE_KAP
    source_company_id: str
    ticker: str
    company_name: str | None = None
    yahoo_ticker: str | None = None


class FilingCandidate(BaseModel):
    """Metadata for one financial-statement notification as listed by the source."""

    model_config = ConfigDict(frozen=True)

    notification_id: str
    published_at: datetime
    fiscal_year: int
    period_end_date: date | None = None
    period_label: str | None = None
    is_annual: bool
    consolidation_scope: ConsolidationScope | None = None
    statement_type: str | None = None
    is_withdrawn: bool = False
    is_correction: bool = False
    source_url: str | None = None
    document_url: str | None = None


class FilingDownload(BaseModel):
    """Bytes and provenance for one filing, before it is written as an immutable snapshot."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    candidate: FilingCandidate
    files: dict[str, bytes]
    content_types: dict[str, str] = Field(default_factory=dict)
    source_urls: dict[str, str] = Field(default_factory=dict)
    validators: dict[str, dict[str, str]] = Field(default_factory=dict)
    retrieved_at: datetime
    not_modified: bool = False  # conditional request answered 304: reuse the stored snapshot


class FundamentalRecord(BaseModel):
    """One current or comparative annual period inside one parsed report version."""

    fiscal_year: int
    fiscal_period: int = ANNUAL_PERIOD
    period_start_date: date | None = None
    period_end_date: date
    is_comparative: bool
    currency_code: str
    currency_scale: int
    presentation_currency_raw: str

    total_liabilities_and_equity: Decimal | None = None
    profit_attributable_to_non_controlling_interests: Decimal | None = None
    profit_attributable_to_owners_of_parent: Decimal | None = None
    current_liabilities: Decimal | None = None
    non_current_liabilities: Decimal | None = None
    total_equity: Decimal | None = None
    total_assets: Decimal | None = None
    revenue: Decimal | None = None
    net_profit: Decimal | None = None
    finance_sector_revenue: Decimal | None = None

    eps: Decimal | None = None
    operating_income: Decimal | None = None
    cash_and_cash_equivalents: Decimal | None = None
    total_debt: Decimal | None = None
    ebitda: Decimal | None = None
    free_cash_flow: Decimal | None = None
    shares_outstanding: Decimal | None = None

    total_debt_method: str | None = None
    ebitda_method: str | None = None
    free_cash_flow_method: str | None = None
    shares_outstanding_method: str | None = None

    def financial_values(self) -> dict[str, Decimal | None]:
        return {name: getattr(self, name) for name in FINANCIAL_FIELDS}


class ParsedReport(BaseModel):
    """Output of the parser for one raw snapshot and one parser version."""

    parser_version: str
    parse_status: ParseStatus
    filing_fiscal_year: int | None = None
    filing_fiscal_period: int = ANNUAL_PERIOD
    filing_period_start_date: date | None = None
    filing_period_end_date: date | None = None
    consolidation_scope: ConsolidationScope | None = None
    statement_type: str | None = None
    periods: list[FundamentalRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    field_sources: dict[str, Any] = Field(default_factory=dict)
    derivations: dict[str, Any] = Field(default_factory=dict)

    def current_period(self) -> FundamentalRecord | None:
        for record in self.periods:
            if not record.is_comparative:
                return record
        return None

    def validation_summary(self) -> dict[str, Any]:
        return {"status": self.parse_status.value, "errors": self.errors, "warnings": self.warnings}


class ReportRecord(BaseModel):
    """Row shape for dbo.reports."""

    company_id: int
    market_source: str
    notification_id: str
    published_at: datetime
    filing_fiscal_year: int
    filing_fiscal_period: int
    filing_period_start_date: date | None
    filing_period_end_date: date
    consolidation_scope: str
    statement_type: str
    source_url: str | None
    document_url: str | None
    raw_path: str
    content_hash: str
    parser_version: str
    retrieved_at: datetime
    parsed_at: datetime | None
    parse_status: str
    is_withdrawn: bool = False
    validation_summary: str | None = None
