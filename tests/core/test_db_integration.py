"""SQL Server integration checks. Skipped unless STOCK_CRAWLER_TEST_MSSQL_URL points at a DISPOSABLE
database whose schema was created with `stock-crawler init-db`. The test truncates the three tables, so
the URL must use an account with DELETE rights (the bootstrap account) - crawler_writer deliberately
cannot delete. Run it inside the crawler container, where the ODBC driver is installed:

  docker compose run --rm -e STOCK_CRAWLER_TEST_MSSQL_URL="$URL" -v "$PWD/tests:/app/tests:ro" \
      --entrypoint sh crawler -c "pip install -q --user pytest && python -m pytest tests/test_db_integration.py"
"""

import os
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from stock_crawler.core.db import Repository, split_batches
from stock_crawler.core.models import CompanyIdentity, FundamentalRecord, ReportRecord

URL = os.environ.get("STOCK_CRAWLER_TEST_MSSQL_URL")
pytestmark = pytest.mark.integration


def test_schema_splits_into_batches():
    from stock_crawler.crawl.cli import SCHEMA_PATH

    batches = split_batches(SCHEMA_PATH.read_text("utf-8"))
    assert any("CREATE OR ALTER VIEW dbo.vw_fundamentals" in b for b in batches)
    assert all("\nGO" not in b for b in batches)


@pytest.fixture
def repo():
    if not URL:
        pytest.skip("STOCK_CRAWLER_TEST_MSSQL_URL not set")
    engine = create_engine(URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.fundamentals; DELETE FROM dbo.reports; DELETE FROM dbo.companies;"))
    yield Repository(engine)
    engine.dispose()


def _report(company_id, notification_id, content_hash, parser_version, published):
    return ReportRecord(
        company_id=company_id, market_source="kap", notification_id=notification_id, published_at=published,
        filing_fiscal_year=2024, filing_fiscal_period=4, filing_period_start_date=date(2024, 1, 1), filing_period_end_date=date(2024, 12, 31),
        consolidation_scope="consolidated", statement_type="general", source_url=None, document_url=None,
        raw_path=f"raw/kap/c/{notification_id}/{content_hash}", content_hash=content_hash, parser_version=parser_version,
        retrieved_at=published, parsed_at=published, parse_status="valid",
    )


def _row(revenue):
    return FundamentalRecord(fiscal_year=2024, period_end_date=date(2024, 12, 31), is_comparative=False, currency_code="TRY", currency_scale=1000, presentation_currency_raw="1000TL", revenue=Decimal(revenue), total_assets=Decimal(1))


def test_views_select_latest_version_and_exclude_withdrawn(repo):
    company = repo.upsert_company(CompanyIdentity(source_company_id="c1", ticker="THYAO", company_name="T"))
    t0 = datetime(2025, 3, 5, 15, 45, tzinfo=timezone.utc)
    first, outcome = repo.save_report(_report(company.company_id, "100", "a" * 64, "1.0.0", t0), [_row(700)])
    assert outcome == "inserted"
    assert repo.save_report(_report(company.company_id, "100", "a" * 64, "1.0.0", t0), [_row(700)])[1] == "exists"
    repo.save_report(_report(company.company_id, "100", "a" * 64, "1.1.0", t0), [_row(701)])
    assert repo.current_view_row(company.company_id, 2024, "consolidated")["revenue"] == Decimal(701)
    repo.save_report(_report(company.company_id, "101", "b" * 64, "1.0.0", datetime(2025, 3, 20, tzinfo=timezone.utc)), [_row(705)])
    assert repo.current_view_row(company.company_id, 2024, "consolidated")["revenue"] == Decimal(705)
    assert repo.mark_withdrawn("kap", "101") == 1
    assert repo.current_view_row(company.company_id, 2024, "consolidated")["revenue"] == Decimal(701)
    assert repo.get_fundamentals(first)[0]["revenue"] == Decimal(700)
