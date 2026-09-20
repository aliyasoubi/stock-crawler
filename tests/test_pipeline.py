"""End-to-end sync/reprocess against the fixture source and the in-memory repository."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from stock_crawler.fetch import AccessBlocked, BudgetExhausted
from stock_crawler.kap import FixtureSourceClient, UnknownTicker
from stock_crawler.models import ParseStatus
from stock_crawler.parser import PARSER_VERSION

# A version that is guaranteed to differ from the current parser, whatever it is.
NEXT_PARSER_VERSION = PARSER_VERSION + ".next"
from stock_crawler.pipeline import Pipeline, SyncOptions
from stock_crawler.storage import RawStore, StateStore


def statuses(summary):
    return {c["ticker"]: c["status"] for c in summary.data["companies"]}


def test_sync_captures_parses_and_publishes(make_pipeline, repo, settings):
    summary = make_pipeline().sync(SyncOptions(tickers=["ASELS", "THYAO"]))
    assert statuses(summary) == {"ASELS": "published", "THYAO": "published"}
    thyao = repo.get_company_by_ticker("kap", "THYAO")
    assert thyao.latest_discovered_notification_id == "1400001" and thyao.last_success_at is not None and thyao.last_error is None
    row = repo.current_view_row(thyao.company_id, 2024, "consolidated")
    assert row["revenue"] == Decimal(700_000_000) and row["currency_scale"] == 1000
    asels_row = repo.current_view_row(repo.get_company_by_ticker("kap", "ASELS").company_id, 2024, "consolidated")
    assert asels_row["revenue"] == Decimal(700_000) and asels_row["currency_scale"] == 1
    raw = RawStore(settings.data_dir)
    ref = raw.latest_snapshot("kap", thyao.source_company_id)
    assert ref.notification_id == "1400001" and (ref.path / "parsed" / f"{PARSER_VERSION}.json").is_file()
    report = repo.get_report(row["report_id"])
    assert report["raw_path"] == ref.relative_path and report["parse_status"] == "valid"
    assert summary.path.is_file() and json.loads(summary.path.read_text())["request_attempts"] == 0


def test_rerun_is_fresh_and_refresh_reuses_snapshot_without_duplicates(make_pipeline, repo, settings, clock):
    pipeline = make_pipeline()
    pipeline.sync(SyncOptions(tickers=["THYAO"]))
    second = pipeline.sync(SyncOptions(tickers=["THYAO"]))
    assert statuses(second) == {"THYAO": "fresh"}
    third = pipeline.sync(SyncOptions(tickers=["THYAO"], refresh=True))
    entry = third.data["companies"][0]
    assert entry["status"] == "already_parsed" and entry["snapshot"].startswith("reused")
    clock.advance(hours=25)
    fourth = pipeline.sync(SyncOptions(tickers=["THYAO"]))
    assert fourth.data["companies"][0]["status"] == "already_parsed"
    clock.advance(hours=200)
    fifth = pipeline.sync(SyncOptions(tickers=["THYAO"]))
    assert fifth.data["companies"][0]["snapshot"] == "unchanged (same content hash)"
    assert len(repo.reports) == 1 and len(RawStore(settings.data_dir).list_snapshots("kap", "fixture-thyao-0001")) == 1


def test_source_content_change_without_financial_delta(make_pipeline, repo, fixture_copy, clock):
    pipeline = make_pipeline(fixture_copy)
    pipeline.sync(SyncOptions(tickers=["THYAO"]))
    page = fixture_copy / "THYAO" / "1400001" / "source.html"
    page.write_text(page.read_text().replace("<title>", "<title>Updated 2025 "))
    clock.advance(hours=200)
    summary = pipeline.sync(SyncOptions(tickers=["THYAO"]))
    entry = summary.data["companies"][0]
    assert entry["snapshot"] == "changed (new content hash)" and entry["status"] == "published"
    assert entry["changed_fields"] == [] and entry["previous_report_id"] == 1
    assert len(repo.reports) == 2 and {r["content_hash"] for r in repo.reports.values()}.__len__() == 2


def test_correction_keeps_both_versions_and_reports_delta(make_pipeline, repo, fixture_copy, clock):
    pipeline = make_pipeline(fixture_copy)
    pipeline.sync(SyncOptions(tickers=["THYAO"]))
    filings_path = fixture_copy / "THYAO" / "filings.json"
    filings = json.loads(filings_path.read_text())
    corrected_dir = fixture_copy / "THYAO" / "1400009"
    corrected_dir.mkdir()
    original = (fixture_copy / "THYAO" / "1400001" / "source.html").read_text()
    (corrected_dir / "source.html").write_text(original.replace("<td>Hasılat</td><td>700.000</td>", "<td>Hasılat</td><td>705.000</td>"))
    filings.append({**next(f for f in filings if f["notification_id"] == "1400001"), "notification_id": "1400009", "published_at": "2025-03-20T09:00:00+03:00", "is_correction": True, "files": {"source.html": "1400009/source.html"}})
    filings_path.write_text(json.dumps(filings))
    clock.advance(hours=25)
    summary = pipeline.sync(SyncOptions(tickers=["THYAO"]))
    entry = summary.data["companies"][0]
    assert entry["notification_id"] == "1400009" and entry["changed_fields"] == ["revenue"]
    company = repo.get_company_by_ticker("kap", "THYAO")
    assert repo.current_view_row(company.company_id, 2024, "consolidated")["revenue"] == Decimal(705_000_000)
    assert len(repo.reports) == 2 and repo.get_fundamentals(1)[0]["revenue"] == Decimal(700_000_000)


def test_reprocess_with_new_parser_version_retains_prior_version(make_pipeline, repo, settings):
    make_pipeline().sync(SyncOptions(tickers=["THYAO"]))
    summary = make_pipeline(parser_version=NEXT_PARSER_VERSION).reprocess(["THYAO", "BIMAS"])
    by_ticker = {c["ticker"]: c for c in summary.data["companies"]}
    assert by_ticker["THYAO"]["status"] == "published" and by_ticker["THYAO"]["parser_version"] == NEXT_PARSER_VERSION
    assert by_ticker["BIMAS"]["status"] == "cache_miss"
    assert {r["parser_version"] for r in repo.reports.values()} == {PARSER_VERSION, NEXT_PARSER_VERSION}
    company = repo.get_company_by_ticker("kap", "THYAO")
    assert repo.current_view_row(company.company_id, 2024, "consolidated")["parser_version"] == NEXT_PARSER_VERSION
    assert summary.data["request_attempts"] == 0
    again = make_pipeline(parser_version=NEXT_PARSER_VERSION).reprocess(["THYAO"])
    assert again.data["companies"][0]["status"] == "already_parsed"


def test_reprocess_reports_missing_snapshot_files(make_pipeline, repo, settings):
    make_pipeline().sync(SyncOptions(tickers=["THYAO"]))
    ref = RawStore(settings.data_dir).latest_snapshot("kap", "fixture-thyao-0001")
    (ref.path / "source.html").unlink()
    summary = make_pipeline(parser_version="2.0.0").reprocess(["THYAO"])
    assert summary.data["companies"][0]["status"] == "cache_miss"


def test_failed_parse_is_visible_and_never_fabricates_rows(make_pipeline, repo, fixture_copy, settings):
    page = fixture_copy / "THYAO" / "1400001" / "source.html"
    page.write_text(page.read_text().replace("<td>1000TL</td>", "<td>Bin Lira</td>"))
    summary = make_pipeline(fixture_copy).sync(SyncOptions(tickers=["THYAO"]))
    entry = summary.data["companies"][0]
    assert entry["status"] == "failed" and entry["persist"] == "inserted"
    company = repo.get_company_by_ticker("kap", "THYAO")
    assert company.last_error.startswith("parse failed") and company.last_success_at is None
    assert repo.get_fundamentals(entry["report_id"]) == [] and repo.current_view_row(company.company_id, 2024, "consolidated") is None
    assert RawStore(settings.data_dir).latest_snapshot("kap", "fixture-thyao-0001") is not None  # evidence preserved for offline reproduction


def test_newer_invalid_filing_leaves_previous_valid_data_readable(make_pipeline, repo, fixture_copy, clock):
    pipeline = make_pipeline(fixture_copy)
    pipeline.sync(SyncOptions(tickers=["THYAO"]))
    filings_path = fixture_copy / "THYAO" / "filings.json"
    filings = json.loads(filings_path.read_text())
    broken_dir = fixture_copy / "THYAO" / "1500001"
    broken_dir.mkdir()
    (broken_dir / "source.html").write_text("<html><body><p>nothing here</p></body></html>")
    filings.append({**filings[2], "notification_id": "1500001", "fiscal_year": 2025, "period_end_date": "2025-12-31", "published_at": "2026-03-05T18:00:00+03:00", "files": {"source.html": "1500001/source.html"}})
    filings_path.write_text(json.dumps(filings))
    clock.advance(hours=25)
    summary = pipeline.sync(SyncOptions(tickers=["THYAO"]))
    assert summary.data["companies"][0]["status"] == "failed"
    company = repo.get_company_by_ticker("kap", "THYAO")
    assert company.latest_discovered_notification_id == "1500001" and "parse failed" in company.last_error
    assert repo.current_view_row(company.company_id, 2024, "consolidated")["revenue"] == Decimal(700_000_000)


def test_withdrawal_marks_every_stored_version(make_pipeline, repo, fixture_copy, clock):
    pipeline = make_pipeline(fixture_copy)
    pipeline.sync(SyncOptions(tickers=["THYAO"]))
    filings_path = fixture_copy / "THYAO" / "filings.json"
    filings = [f for f in json.loads(filings_path.read_text()) if f["is_annual"] is False or f["notification_id"] == "1400001"]
    for filing in filings:
        if filing["notification_id"] == "1400001":
            filing["is_withdrawn"] = True
    filings_path.write_text(json.dumps(filings))
    clock.advance(hours=25)
    summary = pipeline.sync(SyncOptions(tickers=["THYAO"]))
    entry = summary.data["companies"][0]
    assert entry["withdrawn"] == ["1400001"] and entry["status"] == "no_filing"
    company = repo.get_company_by_ticker("kap", "THYAO")
    assert repo.current_view_row(company.company_id, 2024, "consolidated") is None
    assert repo.get_report(1)["is_withdrawn"] is True
    assert repo.get_fundamentals(1)[0]["revenue"] == Decimal(700_000_000)  # values preserved


def test_unknown_ticker_is_reported_not_guessed(make_pipeline):
    summary = make_pipeline().sync(SyncOptions(tickers=["TUPRS", "THYAO"]))
    assert statuses(summary) == {"TUPRS": "unresolved", "THYAO": "published"}
    assert "not present" in summary.data["companies"][0]["error"]


def test_limit_prioritises_never_checked_then_oldest(make_pipeline, repo, clock):
    pipeline = make_pipeline()
    pipeline.sync(SyncOptions(tickers=["THYAO"]))
    clock.advance(hours=30)
    summary = pipeline.sync(SyncOptions(tickers=["ASELS", "THYAO"], limit=1))
    assert statuses(summary) == {"THYAO": "deferred", "ASELS": "published"}
    clock.advance(hours=30)
    summary = pipeline.sync(SyncOptions(tickers=["ASELS", "THYAO"], limit=1))
    assert statuses(summary) == {"ASELS": "deferred", "THYAO": "already_parsed"}


class StoppingSource(FixtureSourceClient):
    def __init__(self, root, error, **kw):
        super().__init__(root, **kw)
        self.error = error
        self.calls = 0

    def list_financial_filings(self, identity):
        self.calls += 1
        if self.calls == 2:
            raise self.error
        return super().list_financial_filings(identity)


@pytest.mark.parametrize("error", [BudgetExhausted("budget"), AccessBlocked("blocked")])
def test_source_wide_stop_ends_run_and_lists_pending(settings, repo, clock, error):
    from tests.conftest import FIXTURE_ROOT

    source = StoppingSource(FIXTURE_ROOT, error, clock=clock)
    pipeline = Pipeline(settings, repo, RawStore(settings.data_dir), StateStore(settings.data_dir, clock=clock), source, clock=clock)
    summary = pipeline.sync(SyncOptions(tickers=["ASELS", "THYAO"]))
    assert statuses(summary) == {"ASELS": "published", "THYAO": "stopped"}
    assert summary.data["stopped_reason"] and summary.data["pending"] == ["THYAO"]
    company = repo.get_company_by_ticker("kap", "THYAO")
    assert company.last_error is not None and company.last_discovery_at is not None


def test_failed_company_is_eligible_on_immediate_retry(make_pipeline, fixture_copy):
    page = fixture_copy / 'THYAO' / '1400001' / 'source.html'
    original = page.read_bytes()
    page.write_bytes(b'<html>invalid source</html>')
    pipeline = make_pipeline(fixture_copy)
    assert statuses(pipeline.sync(SyncOptions(tickers=['THYAO'])))['THYAO'] == 'failed'
    # The retry must not claim that this failure is a fresh successful discovery.
    page.write_bytes(original)
    second = pipeline.sync(SyncOptions(tickers=['THYAO']))
    assert statuses(second)['THYAO'] == 'published'


def test_nonpositive_limit_rejected_before_work(make_pipeline):
    with pytest.raises(ValueError, match='positive'):
        make_pipeline().sync(SyncOptions(tickers=['THYAO'], limit=-1))
