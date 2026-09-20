"""Failures exposed by the September 19 batch and review patch."""
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from stock_crawler.compare import compare_rows
from stock_crawler.kap import UnknownTicker
from stock_crawler.kap_export import CompanyRegistry
from stock_crawler.main import summary_exit_code, _print_summary
from stock_crawler.pipeline import Pipeline, SyncOptions
from stock_crawler.storage import RawStore, RunSummary, StateStore
from .test_kap_export import BOOK, NEXT_PARSER_VERSION, REGISTRY, _mock_export_client, modified_book


@pytest.mark.parametrize('title,ticker', [
    ('ARSAN TEKSTİL TİCARET VE SANAYİ A.Ş.', 'ARSAN'),
    ('ZORLU FAKTORİNG A.Ş.', 'ARSNF'),
])
def test_verified_historical_names_resolve_only_in_the_requested_batch(title, ticker):
    registry = CompanyRegistry(REGISTRY)
    assert registry.match(title, [ticker]) == registry.resolve(ticker)
    with pytest.raises(UnknownTicker):
        registry.match(title, ['ASELS'])


def test_old_arsan_name_publishes_without_fuzzy_matching(settings, repo, clock):
    def change(sheet):
        for number in (8, 9):
            sheet.cell(number, 1, 'ARSAN TEKSTİL TİCARET VE SANAYİ A.Ş.')
    requests = []
    source, state, fetcher = _mock_export_client(settings, clock, modified_book(change), requests)
    # Identity-only synthetic fixture: amounts remain ASELS's, not ARSAN financial data.
    summary = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock).sync(
        SyncOptions(tickers=['ARSAN', 'THYAO']))
    assert len(repo.reports) == 4
    assert summary.data['rejected_row_count'] == 0
    assert summary_exit_code(summary) == 0
    fetcher.close()


def test_all_conflicting_notification_rows_are_quarantined(settings, repo, clock):
    def change(sheet):
        duplicate = [cell.value for cell in sheet[8]]
        duplicate[15] = '999.000'
        sheet.append(duplicate)
    requests = []
    source, state, fetcher = _mock_export_client(settings, clock, modified_book(change), requests)
    summary = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock).sync(
        SyncOptions(tickers=['ASELS', 'THYAO']))
    rejected_ids = {row['notification_id'] for row in summary.data['rejected_rows']}
    assert summary.data['rejected_row_count'] == 2 and len(rejected_ids) == 1
    assert len(repo.reports) == 3
    assert all(r['notification_id'] not in rejected_ids for r in repo.reports.values())
    assert summary_exit_code(summary) == 1
    fetcher.close()


def test_rejected_rows_make_an_otherwise_successful_run_nonzero(tmp_path, capsys):
    summary = RunSummary(tmp_path, 'sync')
    summary.add_company({'ticker': 'THYAO', 'status': 'published'})
    summary.data.update(rejected_rows=[{'company': 'Unmatched issuer', 'reason': 'UnknownTicker'}], rejected_row_count=1)
    assert summary_exit_code(summary) == 1
    _print_summary(summary)
    assert 'rejected rows: 1' in capsys.readouterr().out


def test_missing_requested_year_is_visible_and_counts_as_checked(settings, repo, clock):
    """A year the source answered with no row is an explicit `no_filing` result. It is then
    treated as checked: re-asking within the discovery interval returned the same empty answer
    on every September 19 run and consumed ~40% of the request budget. After the interval,
    or with --refresh, the company is due again."""
    def change(sheet):
        for number in range(sheet.max_row, 7, -1):
            if str(sheet.cell(number, 4).value) == '2023':
                sheet.delete_rows(number)
    requests = []
    source, state, fetcher = _mock_export_client(settings, clock, modified_book(change), requests)
    pipeline = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock)
    first = pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO']))
    assert {(r['ticker'], r['fiscal_year']) for r in first.data['companies'] if r['status'] == 'no_filing'} == {
        ('ASELS', 2023), ('THYAO', 2023)}
    assert summary_exit_code(first) == 1
    second = pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO']))
    assert len(requests) == 1 and all(r['status'] == 'fresh' for r in second.data['companies'])
    third = pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO'], refresh=True))
    assert len(requests) == 2 and sum(r['status'] == 'no_filing' for r in third.data['companies']) == 2
    clock.advance(hours=25)
    fourth = pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO']))
    assert len(requests) == 3 and not any(r['status'] == 'fresh' for r in fourth.data['companies'])
    fetcher.close()


def test_unsupported_and_failed_years_count_as_checked_until_the_interval_elapses(settings, repo, clock):
    def change(sheet):
        sheet.cell(8, 8, 'bank')          # ASELS 2023 -> unsupported format
        sheet.cell(9, 16).value = None    # ASELS 2024 -> no revenue
        sheet.cell(9, 18).value = None    #              and no finance-sector revenue -> failed
    requests = []
    source, state, fetcher = _mock_export_client(settings, clock, modified_book(change), requests)
    pipeline = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock)
    first = pipeline.sync(SyncOptions(tickers=['ASELS']))
    assert sorted(r['status'] for r in first.data['companies']) == ['failed', 'unsupported']
    second = pipeline.sync(SyncOptions(tickers=['ASELS']))
    assert len(requests) == 1 and [r['status'] for r in second.data['companies']] == ['fresh']
    fetcher.close()


def test_new_run_clears_rejections_and_failed_batch_cache(settings, repo, clock):
    requests = []
    source, state, fetcher = _mock_export_client(settings, clock, BOOK.read_bytes(), requests)
    pipeline = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock)
    first = pipeline.sync(SyncOptions(tickers=['THYAO']))
    assert first.data['rejected_row_count'] == 2  # unrequested ASELS rows
    source._failed['ASELS'] = 'failure from an earlier run'
    second = pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO'], refresh=True))
    assert second.data['rejected_row_count'] == 0
    assert summary_exit_code(second) == 0
    assert len(requests) == 2
    fetcher.close()


def test_each_year_expires_independently(tmp_path, clock):
    state = StateStore(tmp_path, clock=clock)
    state.add_coverage('issuer/parser', [2020])
    clock.advance(hours=25)
    state.add_coverage('issuer/parser', [2025])
    assert state.coverage('issuer/parser') == [2020, 2025]
    assert state.fresh_coverage('issuer/parser', now=clock(), max_age=timedelta(hours=24)) == {2025}


def test_legacy_coverage_without_year_timestamps_is_due(tmp_path, clock):
    state = StateStore(tmp_path, clock=clock)
    state.root.mkdir(parents=True)
    (state.root / 'revalidation.json').write_text(json.dumps({
        'coverage:old': {'years': [2024], 'last_collected_at': clock().isoformat()}}))
    assert state.fresh_coverage('old', now=clock(), max_age=timedelta(hours=24)) == set()


def test_parser_change_is_applied_offline_by_reprocess_not_by_refetching(settings, repo, clock):
    """Fetch coverage belongs to the source data. A parser upgrade must not re-download 754
    companies whose bytes are already on disk; `reprocess` re-parses them with zero HTTP, and a
    company that is fetched anyway (interval elapsed) is re-parsed on the spot."""
    requests = []
    source, state, fetcher = _mock_export_client(settings, clock, BOOK.read_bytes(), requests)
    pipeline = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock)
    pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO']))
    assert all(r['status'] == 'fresh' for r in pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO'])).data['companies'])
    pipeline.parser_version = NEXT_PARSER_VERSION
    assert all(r['status'] == 'fresh' for r in pipeline.sync(SyncOptions(tickers=['ASELS', 'THYAO'])).data['companies'])
    assert len(requests) == 1
    reparsed = pipeline.reprocess(['ASELS', 'THYAO']).data['companies']
    assert reparsed and all(r['status'] == 'published' and r['parser_version'] == NEXT_PARSER_VERSION for r in reparsed)
    assert len(requests) == 1
    fetcher.close()


def test_unexpected_crash_still_writes_current_run_summary(make_pipeline, settings, monkeypatch):
    pipeline = make_pipeline()
    def broken(*args):
        raise NameError('simulated logger failure')
    monkeypatch.setattr(pipeline.source, 'list_financial_filings', broken)
    with pytest.raises(NameError):
        pipeline.sync(SyncOptions(tickers=['THYAO']))
    paths = list((settings.data_dir / 'runs').glob('*/summary.json'))
    assert len(paths) == 1
    saved = json.loads(paths[0].read_text())
    assert saved['error_type'] == 'NameError' and saved['finished_at']
    assert saved['stopped_reason'].startswith('unexpected NameError')


def test_configured_cap_is_reported_even_if_cli_limit_is_larger(settings, repo, clock, caplog):
    requests = []
    source, state, fetcher = _mock_export_client(settings, clock, BOOK.read_bytes(), requests)
    settings.max_companies_per_run = 1
    summary = Pipeline(settings, repo, RawStore(settings.data_dir), state, source, clock=clock).sync(
        SyncOptions(tickers=['ASELS', 'THYAO'], limit=100))
    assert summary.data['effective_company_limit'] == 1
    assert summary.data['selected_company_count'] == 1
    assert 'MAX_COMPANIES_PER_RUN=1' in caplog.text
    assert any(r['status'] == 'deferred' for r in summary.data['companies'])
    fetcher.close()


def test_different_measuring_units_suppress_monetary_delta():
    before = dict(fiscal_year=2023, fiscal_period=4, is_comparative=True,
                  currency_code='TRY', measuring_unit_date='2024-12-31', revenue=Decimal(100))
    after = {**before, 'measuring_unit_date': '2025-12-31', 'revenue': Decimal(150)}
    diffs = {d.field: d for d in compare_rows([before], [after])[0].diffs}
    assert diffs['revenue'].delta is None
    assert diffs['measuring_unit_date'].after == '2025-12-31'
