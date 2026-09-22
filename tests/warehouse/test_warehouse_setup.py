"""Offline behavior tests; SQLite reference inserts do not verify SQL Server DDL."""
from datetime import date
from pathlib import Path
import pytest
from sqlalchemy import Column, Integer, String, Identity, MetaData, Table, create_engine, select
from stock_crawler.warehouse.admin import warehouse_settings, insert_master
from stock_crawler.warehouse.loader import load_bundle, should_replace, validate_record
from stock_crawler.warehouse.cli import main
from stock_crawler.warehouse.sources import import_tcmb_fx, tcmb_fx_url
from tests.warehouse.test_warehouse import fundamentals


def test_warehouse_file_wins_over_docker_environment(tmp_path, monkeypatch):
    for key in ('MSSQL_HOST', 'MSSQL_DATABASE', 'MSSQL_PASSWORD', 'MSSQL_USER'):
        monkeypatch.setenv(key, 'wrong')
    monkeypatch.setenv('MSSQL_TRUST_SERVER_CERTIFICATE', 'true')
    env = tmp_path / 'warehouse.env'
    env.write_text('MSSQL_HOST=warehouse\nMSSQL_PORT=1433\nMSSQL_DATABASE=target\nMSSQL_USER=loader\nMSSQL_PASSWORD=literal${secret}\n')
    settings = warehouse_settings(env)
    assert (settings.mssql_host, settings.mssql_database, settings.mssql_user) == ('warehouse', 'target', 'loader')
    assert settings.mssql_password.get_secret_value() == 'literal${secret}'
    assert not settings.mssql_trust_server_certificate
    env.write_text('MSSQL_DATABASE=target\n')
    with pytest.raises(ValueError, match='explicitly define'):
        warehouse_settings(env)


def test_warehouse_defaults_to_application_environment(monkeypatch):
    values = {
        'MSSQL_HOST': '127.0.0.1',
        'MSSQL_PORT': '1433',
        'MSSQL_DATABASE': 'StockFundamentals',
        'MSSQL_USER': 'crawler_writer',
        'MSSQL_PASSWORD': 'writer-secret',
        'MSSQL_BOOTSTRAP_USER': 'sa',
        'MSSQL_BOOTSTRAP_PASSWORD': 'admin-secret',
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    runtime = warehouse_settings()
    admin = warehouse_settings(use_bootstrap=True)
    assert (runtime.mssql_host, runtime.mssql_user,
            runtime.mssql_password.get_secret_value()) == (
                '127.0.0.1', 'crawler_writer', 'writer-secret')
    assert (admin.mssql_host, admin.mssql_user,
            admin.mssql_password.get_secret_value()) == (
                '127.0.0.1', 'sa', 'admin-secret')


@pytest.mark.parametrize('identity', [False, True])
def test_reference_seed_preserves_ids_and_is_repeatable(identity):
    engine = create_engine('sqlite://')
    md = MetaData()
    pk = Column('CompanyId', Integer, Identity(), primary_key=True) if identity else Column('CompanyId', Integer, primary_key=True)
    table = Table('Company', md, pk, Column('Ticker', String, nullable=False),
                  Column('MarketId', Integer, nullable=False), Column('FullName', String, nullable=False), Column('IsActive', Integer))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(CompanyId=42, Ticker='ASELS', MarketId=7, FullName='Existing verified name', IsActive=1))
        row, created = insert_master(conn, table, {'Ticker': 'ASELS', 'MarketId': 7, 'FullName': 'Registry name'}, ['MarketId', 'Ticker'])
        assert not created and row['CompanyId'] == 42 and row['FullName'] == 'Existing verified name'
        new = {'Ticker': 'THYAO', 'MarketId': 7, 'FullName': 'Registry company'}
        row, created = insert_master(conn, table, new, ['MarketId', 'Ticker'])
        assert created and row['CompanyId'] == 43 and row['IsActive'] is None
        assert not insert_master(conn, table, new, ['MarketId', 'Ticker'])[1]
        assert len(conn.execute(select(table)).all()) == 2


def test_strict_company_schema_does_not_get_guessed_status():
    engine = create_engine('sqlite://')
    md = MetaData()
    table = Table('Company', md, Column('CompanyId', Integer, primary_key=True),
                  Column('Ticker', String, nullable=False), Column('IsActive', Integer, nullable=False))
    md.create_all(engine)
    with engine.begin() as conn, pytest.raises(ValueError, match='IsActive'):
        insert_master(conn, table, {'Ticker': 'ASELS'}, ['Ticker'])


def test_partial_financials_are_explicit_and_still_validate_source():
    report = fundamentals(company_map={'ASELS': 123})
    assert load_bundle(report)['ready'] == 0
    partial = load_bundle(report, allow_partial=True)
    assert partial['ready'] == partial['partial_fundamentals'] == 4
    report['records'][0]['validation_issues'].append('currency_mismatch')
    assert load_bundle(report, allow_partial=True)['ready'] == 3
    report['records'][1]['values']['CompanyId'] = None
    assert load_bundle(report, allow_partial=True)['ready'] == 2


def test_seeded_company_enrichment_cannot_overwrite_known_facts():
    existing = {'CompanyId': 42, 'Ticker': 'ASELS', 'SectorName': None, 'IsActive': True}
    incoming = {'values': dict(existing, SectorName='Verified sector', IsActive=1)}
    assert should_replace('Company', incoming, existing)
    incoming['values']['Ticker'] = 'OTHER'
    assert not should_replace('Company', incoming, existing)
    incoming['values'] = dict(existing, SectorName='Verified sector', IsActive=0)
    assert not should_replace('Company', incoming, existing)


def test_missing_master_details_are_not_reported_ready():
    for table, values in [('Market', {'MarketId': 1}), ('Company', {'CompanyId': 1}), ('MarketIndexMaster', {'IndexId': 1})]:
        r = validate_record({'table': table, 'values': values, 'source': {'provider': 'test', 'observed_at': '2025-01-01T00:00:00+00:00'}})
        assert r['missing_required_fields']


XML = '<Tarih_Date Tarih="19.09.2025" Date="09/19/2025"><Currency CurrencyCode="USD"><Unit>1</Unit><ForexBuying>41.1234</ForexBuying><ForexSelling>42</ForexSelling></Currency></Tarih_Date>'


def test_tcmb_uses_buying_and_source_date_without_fake_metrics(tmp_path):
    path = tmp_path / 'fx.xml'
    path.write_text(XML)
    result = import_tcmb_fx(path, market_id=7, archive_dir=tmp_path / 'raw')
    row = result['records'][0]
    assert result['summary']['ready'] == 1
    assert row['values'] == {'MarketId': 7, 'AsOfDate': '2025-09-19', 'PeriodType': 'D', 'PublishDate': None, 'FxRateUsd': '41.123400'}
    assert row['source']['series_metadata']['FxRateUsd']['unit'] == 'TRY per USD'
    with pytest.raises(ValueError, match='date differs'):
        import_tcmb_fx(path, market_id=7, archive_dir=tmp_path, expected_date=date(2025, 9, 20))
    assert tcmb_fx_url(date(2025, 9, 19)).endswith('/202509/19092025.xml')


@pytest.mark.parametrize('xml', [XML.replace('41.1234', 'NaN'), XML.replace('<Unit>1', '<Unit>0'), '<html>error</html>', XML.replace('CurrencyCode="USD"', 'CurrencyCode="EUR"')])
def test_tcmb_rejects_invalid_response(tmp_path, xml):
    path = tmp_path / 'fx.xml'
    path.write_text(xml)
    with pytest.raises(ValueError):
        import_tcmb_fx(path, market_id=7, archive_dir=tmp_path)


def test_seed_dry_run_resolves_registry_without_sql(tmp_path, capsys):
    env = tmp_path / 'warehouse.env'
    env.write_text('MSSQL_HOST=unreachable\nMSSQL_PORT=1433\nMSSQL_DATABASE=target\nMSSQL_USER=test\nMSSQL_PASSWORD=test\n')
    root = Path(__file__).parents[2]
    assert main(['seed-reference', '--env-file', str(env), '--tickers', 'ASELS,THYAO', '--registry', str(root / 'config/kap_companies.json')]) == 0
    assert '"company_candidates": 2' in capsys.readouterr().out


def test_seed_dry_run_accepts_docker_environment_without_file(monkeypatch, capsys):
    for key, value in {
        'MSSQL_HOST': '127.0.0.1', 'MSSQL_PORT': '1433',
        'MSSQL_DATABASE': 'StockFundamentals', 'MSSQL_USER': 'crawler_writer',
        'MSSQL_PASSWORD': 'secret',
    }.items():
        monkeypatch.setenv(key, value)
    root = Path(__file__).parents[2]
    assert main(['seed-reference', '--tickers', 'ASELS,THYAO',
                 '--registry', str(root / 'config/kap_companies.json')]) == 0
    assert '"company_candidates": 2' in capsys.readouterr().out


def test_official_tcmb_historical_fixture(tmp_path):
    path = Path(__file__).parents[1] / 'fixtures/warehouse/tcmb-2025-09-19.xml'
    result = import_tcmb_fx(path, market_id=7, archive_dir=tmp_path)
    assert result['summary']['ready'] == 1
    assert result['records'][0]['values']['AsOfDate'] == '2025-09-19'
    assert result['records'][0]['values']['FxRateUsd'] == '41.234400'
