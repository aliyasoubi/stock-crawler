from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest

from stock_crawler.warehouse.fundamentals import build_fundamentals, load_mapping
from stock_crawler.warehouse.loader import (validate_record, import_csv, should_replace, load_bundle,
                                           bundle, is_fatal_error, macro_field_changes)
from stock_crawler.warehouse.sources import import_evds, evds_url, fetch_snapshot, normalize_vendor_csv
from stock_crawler.warehouse.cli import main

ROOT = Path(__file__).parents[2]
SAMPLES = list((ROOT / 'tests/fixtures/warehouse').glob('*.xlsx'))


def fundamentals(**kwargs):
    return build_fundamentals(SAMPLES, registry_path=ROOT / 'config/kap_companies.json',
        calendar_tickers=['ASELS'], **kwargs)


def test_real_samples_merge_four_periods_scale_and_missing():
    result = fundamentals(company_map={'ASELS': 123})
    assert not result['errors']
    assert len(result['records']) == 4
    q1, q2, q3, q4 = result['records']
    assert [r['values']['FiscalQuarter'] for r in result['records']] == [1, 2, 3, 4]
    assert q1['values']['Revenue'] == '22790773000.0000'
    assert q2['values']['Revenue'] == '53710197000.0000'  # YTD, not differenced
    assert q4['values']['OperatingIncome'] == '49145855000.0000'
    assert q4['values']['NetIncome'] == '29917727000.0000'
    assert q4['values']['TotalLiabilities'] == '179801035000.0000'
    assert q1['values']['PeriodEndDate'] == '2025-03-31'
    assert len(q4['source']['workbook_sha256']) == 2
    assert len(q4['missing_required_fields']) == 7
    assert not q4['validation_issues']
    assert q4['values']['SharesOutstanding'] is None


def test_owners_basis_and_company_id_not_invented():
    r = fundamentals(net_income_basis='owners-of-parent')['records'][-1]
    assert r['values']['NetIncome'] == '29949517000.0000'
    assert r['values']['CompanyId'] is None


def test_calendar_confirmation_is_explicit():
    result = build_fundamentals(SAMPLES, registry_path=ROOT / 'config/kap_companies.json')
    assert 'calendar_year_not_confirmed' in result['records'][0]['validation_issues'][0]


def test_currency_mismatch():
    r = fundamentals(currency='USD')['records'][0]
    assert any('currency_mismatch' in i for i in r['validation_issues'])


def test_complementary_columns_do_not_count_as_conflict():
    r = fundamentals()['records'][0]
    assert not any('conflicting' in i for i in r['validation_issues'])


def test_same_notification_conflict_is_quarantined(monkeypatch):
    import stock_crawler.warehouse.fundamentals as module
    real = module.read_export
    counter = 0
    def changed(*args, **kwargs):
        nonlocal counter
        counter += 1
        rows = real(*args, **kwargs)
        if counter == 2:
            rows[0]['Revenue'] = '999'
        return rows
    monkeypatch.setattr(module, 'read_export', changed)
    result = fundamentals()
    assert 'conflicting_same_notification: Revenue' in result['records'][0]['validation_issues']


def test_unsupported_mapping_and_nonmonetary_scale(tmp_path):
    path = tmp_path / 'mapping.json'
    spec = {'Verified EPS': {'target': 'Eps', 'unit': 'per_share', 'scale': '1',
            'evidence': 'synthetic unit test only', 'item_id': 'test_eps', 'definition': 'basic TRY/share'}}
    path.write_text(json.dumps(spec))
    assert load_mapping(path)['Verified EPS']['scale'] == '1'
    del spec['Verified EPS']['scale']
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError):
        load_mapping(path)


def test_eps_not_scaled_by_money_unit(monkeypatch):
    import stock_crawler.warehouse.fundamentals as module
    real = module.read_export
    def extra(*args, **kwargs):
        rows = real(*args, **kwargs)
        for row in rows:
            row['Verified EPS'] = '6,25'
        return rows
    monkeypatch.setattr(module, 'read_export', extra)
    r = fundamentals(mapping={'Verified EPS': {'target': 'Eps', 'unit': 'per_share', 'scale': 1}})['records'][0]
    assert r['values']['Eps'] == '6.2500'


def market_record(priority=1):
    return {'table': 'MarketData', 'values': {'TradeDate': '2025-01-02', 'CompanyId': 3,
        'OpenPrice': '10', 'HighPrice': '12', 'LowPrice': '9', 'ClosePrice': '11',
        'Volume': '100', 'ValueTraded': '1100', 'SourcePriority': priority},
        'source': {'provider': 'test', 'observed_at': '2025-01-03T00:00:00+00:00',
                   'price_basis': 'as_traded', 'currency': 'TRY', 'source_priority': priority}}


def test_ohlc_units_volume_and_overflow():
    assert not validate_record(market_record())['validation_issues']
    row = market_record()
    row['values']['LowPrice'] = '13'
    assert 'invalid OHLC range' in validate_record(row)['validation_issues']
    row['values']['Volume'] = '1.5'
    with pytest.raises(ValueError):
        validate_record(row)
    row = market_record()
    row['values']['ClosePrice'] = '100000000000000'
    assert any('out_of_range' in i for i in validate_record(row)['validation_issues'])


def test_better_source_wins_lower_priority_cannot_overwrite():
    incoming = validate_record(market_record(2))
    existing = validate_record(market_record(1))['values']
    assert not should_replace('MarketData', incoming, existing)
    assert should_replace('MarketData', validate_record(market_record(1)), incoming['values'])
    assert not should_replace('MarketData', validate_record(market_record(1)), existing)


def test_stale_same_priority_is_skipped():
    r = validate_record(market_record())
    assert not should_replace('MarketData', r, r['values'], dict(r['source'], observed_at='2025-01-04T00:00:00+00:00'))


def test_factorstore_refused():
    with pytest.raises(ValueError):
        validate_record({'table': 'FactorStore', 'values': {}})


def test_csv_duplicate_conflict_and_provenance(tmp_path):
    p = tmp_path / 'index.csv'
    p.write_text('TradeDate,IndexId,ClosePrice\n2025-01-02,5,10000\n')
    index_source = {'provider': 'test', 'currency': 'TRY'}
    result = import_csv(p, 'MarketIndexData', source=index_source, archive_dir=tmp_path)
    assert len(result['records'][0]['source']['raw_sha256']) == 64
    assert result['summary']['ready'] == 1
    p.write_text(p.read_text() + '2025-01-02,5,9999\n')
    with pytest.raises(ValueError, match='conflicting'):
        import_csv(p, 'MarketIndexData', source=index_source, archive_dir=tmp_path)


def evds_profile(frequency='M'):
    return {'verified': True, 'catalog_evidence': 'SYNTHETIC TEST; not live validation',
        'base_url': 'https://evds3.tcmb.gov.tr/igmevdsms-dis/',
        'series': [{'target': 'Cpi', 'code': 'TEST.CPI', 'json_key': 'TEST_CPI',
                    'frequency': frequency, 'unit': 'index', 'definition': 'synthetic base=100', 'scale': '1'}]}


def test_evds_missing_not_zero_no_forward_fill_or_fake_publication(tmp_path):
    p = tmp_path / 'evds.json'
    p.write_text(json.dumps({'items': [{'Tarih': '2025-1', 'TEST_CPI': '125.75'}, {'Tarih': '2025-2', 'TEST_CPI': ''}]}))
    result = import_evds(p, evds_profile(), market_id=1, archive_dir=tmp_path)
    assert len(result['records']) == 1
    v = result['records'][0]['values']
    assert v['AsOfDate'] == '2025-01-31'
    assert v['PublishDate'] is None
    assert v['Cpi'] == '125.7500'
    assert 'Gdp' not in v


def test_evds_unverified_profile_and_schema_drift_fail(tmp_path):
    profile = evds_profile()
    profile['verified'] = False
    with pytest.raises(ValueError, match='verify'):
        evds_url(profile, date(2025, 1, 1), date(2025, 2, 1))
    p = tmp_path / 'evds.json'
    p.write_text('{"items":[{"Tarih":"2025-1","WRONG_KEY":123}]}')
    with pytest.raises(ValueError, match='missing series key'):
        import_evds(p, evds_profile(), market_id=1, archive_dir=tmp_path)


def test_fetch_header_key_and_no_redirect(tmp_path, monkeypatch):
    monkeypatch.setenv('EVDS_TEST_KEY', 'secret-test-value')
    def transport(request):
        assert request.headers['key'] == 'secret-test-value'
        assert 'secret-test-value' not in str(request.url)
        return httpx.Response(200, json={'items': []})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = fetch_snapshot('https://evds3.tcmb.gov.tr/example', tmp_path / 'a.json', key_env='EVDS_TEST_KEY', client=client)
    assert 'secret-test-value' not in json.dumps(result)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={'Location': 'https://example.com'}))) as client:
        with pytest.raises(ValueError, match='302'):
            fetch_snapshot('https://evds3.tcmb.gov.tr/example', tmp_path / 'a.json', client=client)


def test_fetch_throttle_does_not_retry(tmp_path):
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(429, headers={'Retry-After': '60'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError, match='throttled'):
            fetch_snapshot('https://www.borsaistanbul.com/example', tmp_path / 'a.csv', client=client)
    assert len(requests) == 1


def test_review_load_never_opens_database():
    result = load_bundle(fundamentals())
    assert result['mode'] == 'dry_run_no_sql'
    assert result['records'] == 4 and result['ready'] == 0


def test_cli_writes_incomplete_report_and_refuses_overwrite(tmp_path):
    out = tmp_path / 'result.json'
    args = ['fundamentals', '--input', *map(str, SAMPLES), '--registry', str(ROOT / 'config/kap_companies.json'),
            '--data-dir', str(tmp_path / 'data'), '--calendar-tickers', 'ASELS', '--output', str(out)]
    assert main(args) == 1
    assert json.loads(out.read_text())['summary']['records'] == 4
    assert main(['load', '--input', str(out), '--output', str(out)]) == 2


def test_browser_quarters_in_resume_identity():
    from stock_crawler.crawl.browser_export import build_browser_script
    from stock_crawler.crawl.kap_export import CompanyRegistry
    identity = CompanyRegistry(ROOT / 'config/kap_companies.json').resolve('ASELS')
    script = build_browser_script([identity], [2025], periods=[1, 2, 3, 4])
    assert '"periods": [1, 2, 3, 4]' in script
    assert 'periods: config.periods || [4]' in script
    assert 'periodList: (config.periods || [4]).map(String)' in script


def test_vendor_locale_and_identity_filter(tmp_path):
    p = tmp_path / 'vendor.csv'
    p.write_text('code;date;close\nXU100;31.01.2025;10.123,45\nOTHER;31.01.2025;5\n')
    profile = {'table': 'MarketIndexData', 'verified': True, 'evidence': 'synthetic',
        'delimiter': ';', 'source': {'provider': 'test'}, 'filters': {'code': ['XU100']},
        'fields': {'IndexId': {'column': 'code', 'lookup': {'XU100': 7}},
                   'TradeDate': {'column': 'date', 'date_format': '%d.%m.%Y'},
                   'ClosePrice': {'column': 'close', 'number_format': 'turkish'}}}
    result = normalize_vendor_csv(p, profile, archive_dir=tmp_path)
    assert result['records'][0]['values']['ClosePrice'] == '10123.4500'
    assert len(result['records']) == 1


def test_vendor_json_preserves_decimal_and_original_hash(tmp_path):
    from stock_crawler.core.storage import sha256_bytes
    path = tmp_path / 'vendor.json'
    path.write_text('{"value":[{"date":"2025-01-02","close":123.4567}]}')
    profile = {'table': 'MarketIndexData', 'format': 'json', 'rows_path': ['value'],
               'verified': True, 'evidence': 'synthetic only', 'source': {'provider': 'test'},
               'fields': {'IndexId': {'constant': 1}, 'TradeDate': {'column': 'date'},
                          'ClosePrice': {'column': 'close'}}}
    row = normalize_vendor_csv(path, profile, archive_dir=tmp_path)['records'][0]
    assert row['values']['ClosePrice'] == '123.4567'
    assert row['source']['raw_sha256'] == sha256_bytes(path.read_bytes())


def test_macro_definition_change_does_not_overwrite():
    old = {'provider': 'test', 'observed_at': '2025-01-01T00:00:00+00:00',
           'series_metadata': {'Cpi': {'unit': 'index 2003=100'}}}
    new = deepcopy(old)
    new['observed_at'] = '2025-02-01T00:00:00+00:00'
    new['series_metadata']['Cpi']['unit'] = 'index 2025=100'
    assert not should_replace('MacroSovereign', {'source': new}, {}, old)


def test_duplicate_bundle_target_keys_refused():
    r = fundamentals()
    r['records'][0]['values']['CompanyId'] = 1
    r['records'].append(deepcopy(r['records'][0]))
    with pytest.raises(ValueError, match='duplicate target key'):
        load_bundle(r)


def test_fcf_and_ebitda_only_from_explicit_matching_inputs(monkeypatch):
    import stock_crawler.warehouse.fundamentals as module
    real = module.read_export
    def extra(*args, **kwargs):
        rows = real(*args, **kwargs)
        for row in rows:
            row.update({'OCF': '5.000', 'CAPEX': '2.000', 'DA': '100'})
        return rows
    monkeypatch.setattr(module, 'read_export', extra)
    mapping = {h: {'target': t, 'unit': 'money'} for h, t in
               [('OCF', 'OperatingCashFlow'), ('CAPEX', 'CapexCashOutflow'), ('DA', 'OperatingDepreciationAmortization')]}
    r = fundamentals(mapping=mapping)['records'][-1]
    assert r['values']['FreeCashFlow'] == '3000000.0000'
    assert r['values']['Ebitda'] == '49145955000.0000'
    assert r['values']['TotalDebtShort'] is None  # liabilities are not borrowing debt


# --- review regressions: batch omissions, scope promotion, currency precedence, --------
# --- per-field macro vintages and history resume identity -----------------------------

class NotSqlServer:
    """Stands in for an engine far enough to prove the batch passed the error gate."""
    dialect = type('d', (), {'name': 'sqlite'})()


def macro_record(field, value, observed, *, market_id=1, as_of='2025-01-31'):
    return validate_record({'table': 'MacroSovereign', 'values': {
        'MarketId': market_id, 'AsOfDate': as_of, 'PeriodType': 'M', field: value},
        'source': {'provider': 'tcmb_evds', 'observed_at': observed,
                   'series_metadata': {field: {'code': f'TP.{field}', 'frequency': 'M',
                                               'unit': 'x', 'definition': 'd', 'scale': '1'}}}})


def test_symbol_omission_does_not_block_the_rest_of_the_batch():
    """F1: one absent symbol must not suppress every other valid row."""
    omission = {'symbol': 'ASELS', 'error': 'symbol_missing_from_provider_response'}
    document = bundle([market_record()], [omission])
    dry = load_bundle(document)
    assert (dry['ready'], dry['source_errors'], dry['source_omissions']) == (1, 0, 1)
    assert dry['omitted_symbols'] == ['ASELS']
    # The omission is recoverable, so the batch reaches SQL instead of being refused.
    with pytest.raises(ValueError, match='SQL Server only'):
        load_bundle(document, engine=NotSqlServer(), apply=True)
    # An unscoped failure still aborts: it says nothing about which rows are trustworthy.
    with pytest.raises(ValueError, match='resolve source errors'):
        load_bundle(bundle([market_record()], [{'error': 'provider rejected the request'}]),
                    engine=NotSqlServer(), apply=True)
    assert is_fatal_error({'error': 'boom'}) and not is_fatal_error({'ticker': 'THYAO', 'error': 'x'})
    assert is_fatal_error({'symbol': 'ASELS', 'error': 'x', 'fatal': True})


def fundamental_source(scope, published, notification):
    return {'provider': 'kap_compare', 'observed_at': '2025-03-01T00:00:00+00:00',
            'currency': 'TRY', 'scope': scope, 'net_income_basis': 'total',
            'published_at': published, 'notification_id': notification}


def test_consolidated_filing_may_take_over_an_unconsolidated_fallback():
    """F3: the default consolidated-else-unconsolidated policy must hold across runs."""
    prior = fundamental_source('unconsolidated', '2025-03-01T00:00:00+00:00', 111)
    later = fundamental_source('consolidated', '2025-03-10T00:00:00+00:00', 222)
    row = {'values': {'Revenue': '10'}, 'source': later}
    assert should_replace('CompanyFundamental', row, {'Revenue': Decimal(5)}, prior)
    # Never the reverse: a newer unconsolidated filing does not demote a consolidated row.
    demotion = {'values': {'Revenue': '10'},
                'source': fundamental_source('unconsolidated', '2025-04-01T00:00:00+00:00', 333)}
    assert not should_replace('CompanyFundamental', demotion,
                              {'Revenue': Decimal(5)}, fundamental_source('consolidated', '2025-03-10T00:00:00+00:00', 222))
    # Nor does an older consolidated filing outrank a newer one already loaded.
    stale = {'values': {'Revenue': '10'},
             'source': fundamental_source('consolidated', '2025-01-01T00:00:00+00:00', 1)}
    assert not should_replace('CompanyFundamental', stale, {'Revenue': Decimal(5)}, prior)


def test_price_currency_is_checked_before_source_priority():
    """F4: priority ranks comparable observations only, never a different currency."""
    tr = market_record(2)
    usd = market_record(1)
    usd['source'] = dict(usd['source'], currency='USD')
    existing = {'SourcePriority': 2, 'ClosePrice': Decimal(11)}
    assert not should_replace('MarketData', validate_record(usd), existing, tr['source'])
    # A better priority in the same currency is still allowed to win.
    assert should_replace('MarketData', validate_record(market_record(1)), existing, tr['source'])
    # And a price feed that declares no currency cannot be compared at all.
    no_currency = market_record()
    no_currency['source'] = {k: v for k, v in no_currency['source'].items() if k != 'currency'}
    assert 'reviewed three-letter price currency required in source metadata' in \
        validate_record(no_currency)['validation_issues']


def test_macro_series_update_independently_of_load_order():
    """F5: an unrelated later capture must not lock an empty metric out of its own row."""
    existing = {'Cpi': Decimal('100'), 'TaxRevenue': None}
    prior = macro_record('Cpi', '100', '2025-02-01T10:00:00+00:00')['source']
    earlier_tax = macro_record('TaxRevenue', '250', '2025-02-01T09:00:00+00:00')
    assert should_replace('MacroSovereign', earlier_tax, existing, prior)
    assert macro_field_changes(earlier_tax, existing, prior) == {'TaxRevenue': '250.0000'}
    # An older capture of a metric that is already published must not overwrite it.
    stale_cpi = macro_record('Cpi', '99', '2025-01-01T00:00:00+00:00')
    prior_with_times = dict(prior, field_observed_at={'Cpi': '2025-02-01T10:00:00+00:00'})
    assert macro_field_changes(stale_cpi, existing, prior_with_times) == {}
    assert not should_replace('MacroSovereign', stale_cpi, existing, prior_with_times)


def test_history_resume_requires_a_matching_request(tmp_path):
    """F6: resume must compare the request, not merely the existence of the output file."""
    from stock_crawler.warehouse.cli import history_request_covered
    from stock_crawler.warehouse.prices import HISTORY_VERSION
    target = tmp_path / 'ASELS.json'
    request = {'symbol': 'ASELS', 'company_id': 1, 'start': '2015-01-01', 'end': '2015-01-31',
               'parser_version': HISTORY_VERSION, 'completed': True}
    target.write_text(json.dumps({'kind': 'warehouse_bundle', 'request': request}))
    covered = lambda **kw: history_request_covered(
        target, kw.get('ticker', 'ASELS'), kw.get('company_id', 1),
        date.fromisoformat(kw.get('start', '2015-01-01')),
        date.fromisoformat(kw.get('end', '2015-01-31')), HISTORY_VERSION)
    assert covered()
    assert not covered(end='2015-02-28')      # a widened range is not already delivered
    assert not covered(company_id=7)          # a different CompanyId map is a different job
    target.write_text(json.dumps({'kind': 'warehouse_bundle',
                                  'request': dict(request, completed=False)}))
    assert not covered()                      # a bundle with no loadable row is refetched
    target.write_text(json.dumps({'kind': 'warehouse_bundle'}))
    assert not covered()                      # bundles written before request manifests


def test_history_run_reports_an_empty_bundle_as_failure(tmp_path, monkeypatch, capsys):
    """F6: a bundle with no loadable row must not exit 0 and must not be resumed past."""
    import stock_crawler.warehouse.prices as prices
    from stock_crawler.warehouse.loader import bundle as make_bundle
    calls = []

    def fake_history(symbol, company_id, **kwargs):
        calls.append(symbol)
        return make_bundle([], [{'symbol': symbol, 'trade_date': '2015-01-05',
                                 'error': 'HG_AOF is zero; share volume cannot be derived'}])

    monkeypatch.setattr(prices, 'build_isyatirim_history', fake_history)
    company_map = tmp_path / 'ids.json'
    company_map.write_text(json.dumps({'ASELS': 1}))
    argv = ['isyatirim-history', '--company-map', str(company_map), '--start', '2015-01-01',
            '--end', '2015-01-31', '--output-dir', str(tmp_path), '--data-dir', str(tmp_path)]
    assert main(argv) == 1
    assert 'no loadable row' in capsys.readouterr().err
    # The failed symbol is retried instead of being skipped forever by file existence.
    assert main(argv) == 1
    assert calls == ['ASELS', 'ASELS']
