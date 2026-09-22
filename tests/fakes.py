"""In-memory stand-ins for the SQL repository, mirroring the view semantics in schema.sql."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from stock_crawler.core.db import CompanyRow, DatabaseError, IdentityConflict, ReportVersionRow
from stock_crawler.core.models import CompanyIdentity, FundamentalRecord, ReportRecord


class InMemoryRepository:
    def __init__(self) -> None:
        self.companies: dict[int, dict[str, Any]] = {}
        self.reports: dict[int, dict[str, Any]] = {}
        self.fundamentals: dict[int, list[dict[str, Any]]] = {}
        self._next_company = 1
        self._next_report = 1

    # companies
    def _row(self, data: dict[str, Any]) -> CompanyRow:
        return CompanyRow(**{k: data.get(k) for k in CompanyRow.__dataclass_fields__})

    def get_company_by_ticker(self, market_source: str, ticker: str) -> CompanyRow | None:
        for data in self.companies.values():
            if data["market_source"] == market_source and data["ticker"] == ticker:
                return self._row(data)
        return None

    def upsert_company(self, identity: CompanyIdentity) -> CompanyRow:
        for data in self.companies.values():
            if data["market_source"] != identity.market_source:
                continue
            if data["ticker"] == identity.ticker and data["source_company_id"] != identity.source_company_id:
                raise IdentityConflict("ticker maps to a different source id")
            if data["source_company_id"] == identity.source_company_id and data["ticker"] != identity.ticker:
                raise IdentityConflict("source id maps to a different ticker")
            if data["ticker"] == identity.ticker:
                data["company_name"] = identity.company_name or data["company_name"]
                return self._row(data)
        company_id = self._next_company
        self._next_company += 1
        self.companies[company_id] = {
            "company_id": company_id,
            "market_source": identity.market_source,
            "source_company_id": identity.source_company_id,
            "ticker": identity.ticker,
            "company_name": identity.company_name,
            "yahoo_ticker": identity.yahoo_ticker,
            "last_discovery_at": None,
            "last_success_at": None,
            "last_error": None,
            "latest_discovered_notification_id": None,
        }
        return self._row(self.companies[company_id])

    def record_discovery(self, company_id: int, *, at: datetime, notification_id: str | None, error: str | None) -> None:
        data = self.companies[company_id]
        data["last_discovery_at"] = at
        data["last_error"] = error
        if notification_id:
            data["latest_discovered_notification_id"] = notification_id

    def record_success(self, company_id: int, *, at: datetime) -> None:
        self.companies[company_id]["last_success_at"] = at
        self.companies[company_id]["last_error"] = None

    # reports
    def find_report_version(self, market_source, notification_id, content_hash, parser_version):
        for data in self.reports.values():
            if (data["market_source"], data["notification_id"], data["content_hash"], data["parser_version"]) == (
                market_source, notification_id, content_hash, parser_version,
            ):
                return ReportVersionRow(data["report_id"], data["parse_status"], data["is_withdrawn"])
        return None

    def save_report(self, report: ReportRecord, fundamentals: list[FundamentalRecord]) -> tuple[int, str]:
        if report.parse_status == "valid" and not fundamentals:
            raise DatabaseError("valid report without fundamentals")
        existing = self.find_report_version(report.market_source, report.notification_id, report.content_hash, report.parser_version)
        if existing is not None:
            if existing.parse_status == "valid" or report.parse_status != "valid":
                return existing.report_id, "exists"
            data = self.reports[existing.report_id]
            data.update(report.model_dump(), report_id=existing.report_id)
            self.fundamentals[existing.report_id] = [{**f.model_dump(), "report_id": existing.report_id} for f in fundamentals]
            return existing.report_id, "upgraded"
        report_id = self._next_report
        self._next_report += 1
        self.reports[report_id] = {**report.model_dump(), "report_id": report_id}
        self.fundamentals[report_id] = [{**f.model_dump(), "report_id": report_id} for f in fundamentals]
        return report_id, "inserted"

    def notification_is_withdrawn(self, market_source: str, notification_id: str) -> bool:
        return any(r["market_source"] == market_source and r["notification_id"] == notification_id and r["is_withdrawn"] for r in self.reports.values())

    def mark_withdrawn(self, market_source: str, notification_id: str) -> int:
        count = 0
        for data in self.reports.values():
            if data["market_source"] == market_source and data["notification_id"] == notification_id and not data["is_withdrawn"]:
                data["is_withdrawn"] = True
                count += 1
        return count

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        return dict(self.reports[report_id]) if report_id in self.reports else None

    def get_fundamentals(self, report_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.fundamentals.get(report_id, [])]

    def current_view_row(self, company_id: int, fiscal_year: int, consolidation_scope: str) -> dict[str, Any] | None:
        """Same ordering as vw_fundamentals: published_at, numeric id, retrieved_at, parsed_at, report_id."""
        candidates = []
        for report in self.reports.values():
            if report["company_id"] != company_id or report["parse_status"] != "valid" or report["is_withdrawn"]:
                continue
            if report["consolidation_scope"] != consolidation_scope:
                continue
            for row in self.fundamentals.get(report["report_id"], []):
                if row["fiscal_year"] == fiscal_year and row["fiscal_period"] == 4 and not row["is_comparative"]:
                    candidates.append((report, row))
        if not candidates:
            return None

        def key(item):
            report, _ = item
            try:
                numeric = int(report["notification_id"])
            except ValueError:
                numeric = -1
            return (report["published_at"], numeric, report["notification_id"], report["retrieved_at"], report["parsed_at"] or datetime.min.replace(tzinfo=report["retrieved_at"].tzinfo), report["report_id"])

        report, row = max(candidates, key=key)
        merged = {**row, **{k: v for k, v in report.items() if k not in row}}
        merged.pop("is_comparative", None)  # vw_fundamentals exposes current rows only, without this column
        return merged

    # helpers for assertions
    def view_rows(self, company_id: int) -> list[dict[str, Any]]:
        rows = []
        seen = set()
        for report in self.reports.values():
            for fund in self.fundamentals.get(report["report_id"], []):
                key = (company_id, fund["fiscal_year"], report["consolidation_scope"])
                if report["company_id"] == company_id and key not in seen and not fund["is_comparative"]:
                    seen.add(key)
                    current = self.current_view_row(company_id, fund["fiscal_year"], report["consolidation_scope"])
                    if current:
                        rows.append(current)
        return rows
