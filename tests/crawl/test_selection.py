from datetime import date, datetime, timezone

from stock_crawler.crawl.kap import select_latest_annual
from stock_crawler.core.models import ConsolidationScope, FilingCandidate


def cand(nid, year, *, annual=True, scope="consolidated", published="2025-03-05T18:45:00+03:00", end=None, withdrawn=False, correction=False):
    return FilingCandidate(
        notification_id=nid, published_at=datetime.fromisoformat(published), fiscal_year=year,
        period_end_date=end or (date(year, 12, 31) if annual else date(year, 9, 30)), is_annual=annual,
        consolidation_scope=ConsolidationScope(scope) if scope else None, is_withdrawn=withdrawn, is_correction=correction,
    )


def test_quarterly_never_displaces_annual_and_latest_year_wins():
    result = select_latest_annual([cand("1", 2023), cand("2", 2024), cand("3", 2025, annual=False, published="2025-08-08T19:00:00+03:00")])
    assert result.selected.notification_id == "2"
    assert result.annual == 2 and result.considered == 3


def test_latest_year_is_chosen_before_scope_preference():
    older_consolidated = cand("10", 2023, scope="consolidated")
    newer_unconsolidated = cand("11", 2024, scope="unconsolidated")
    assert select_latest_annual([older_consolidated, newer_unconsolidated]).selected.notification_id == "11"


def test_consolidated_preferred_within_period():
    result = select_latest_annual([cand("20", 2024, scope="unconsolidated", published="2025-03-06T10:00:00+03:00"), cand("21", 2024, scope="consolidated")])
    assert result.selected.notification_id == "21"


def test_correction_publication_and_numeric_tie_breaker():
    original = cand("30", 2024, published="2025-03-05T18:45:00+03:00")
    correction = cand("31", 2024, published="2025-03-20T09:00:00+03:00", correction=True)
    assert select_latest_annual([correction, original]).selected.notification_id == "31"
    same_time_a = cand("9", 2024)
    same_time_b = cand("10", 2024)
    assert select_latest_annual([same_time_a, same_time_b]).selected.notification_id == "10"


def test_withdrawn_filings_are_excluded_and_reported():
    withdrawn = cand("40", 2024, withdrawn=True, published="2025-04-01T00:00:00+03:00")
    result = select_latest_annual([withdrawn, cand("39", 2024)])
    assert result.selected.notification_id == "39"
    assert [c.notification_id for c in result.withdrawn] == ["40"]


def test_no_annual_or_unknown_scope_returns_reason():
    assert select_latest_annual([cand("1", 2024, annual=False)]).selected is None
    unknown = select_latest_annual([cand("2", 2024, scope=None)])
    assert unknown.selected is None and "scope" in unknown.reason


def test_non_calendar_fiscal_year_end_uses_period_end_date():
    march = cand("50", 2024, end=date(2024, 3, 31))
    december_prior = cand("51", 2023, end=date(2023, 12, 31))
    assert select_latest_annual([december_prior, march]).selected.notification_id == "50"
