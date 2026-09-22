"""Opt-in SQL Server test. Requires an EMPTY disposable database.
Set STOCK_WAREHOUSE_TEST_MSSQL_URL to its SQLAlchemy URL with DDL rights.
Creates and retains test tables/data there. Never point it at your real database.
"""
from copy import deepcopy
import os
import pytest
from sqlalchemy import create_engine, inspect, text
from stock_crawler.warehouse.admin import initialize, seed_reference, export_maps
from stock_crawler.warehouse.loader import load_bundle
from tests.warehouse.test_warehouse import fundamentals


@pytest.mark.integration
def test_sqlserver_warehouse_lifecycle(tmp_path):
    url = os.environ.get('STOCK_WAREHOUSE_TEST_MSSQL_URL')
    if not url:
        pytest.skip('STOCK_WAREHOUSE_TEST_MSSQL_URL not set; SQL Server execution unverified')
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            if inspect(conn).get_table_names(schema='dbo'):
                pytest.fail('Use an EMPTY disposable database; refusing existing tables')
        initialize(engine)
        initialize(engine)
        companies = [{'ticker': 'ASELS', 'company_name': 'ASELSAN'}]
        assert seed_reference(engine, companies)['inserted']['Company'] == 1
        assert seed_reference(engine, companies)['inserted'] == {'Market': 0, 'Company': 0, 'MarketIndexMaster': 0}
        export_maps(engine, tmp_path)
        with engine.connect() as conn:
            company_id = conn.execute(text("SELECT CompanyId FROM dbo.Company WHERE Ticker='ASELS'")).scalar_one()
        document = fundamentals(company_map={'ASELS': company_id})
        assert load_bundle(document, engine=engine, apply=True)['incomplete'] == 4
        # Replaying an incomplete batch must not pretend to be a successful load.
        replay = load_bundle(document, engine=engine, apply=True)
        assert replay['mode'] == 'already_loaded' and replay['incomplete'] == 4
        assert load_bundle(document, engine=engine, apply=True, allow_partial=True)['inserted'] == 4
        assert load_bundle(document, engine=engine, apply=True, allow_partial=True)['mode'] == 'already_loaded'
        enriched = deepcopy(document)
        for row in enriched['records']:
            row['values']['CashAndEquivalents'] = '123.0000'
        assert load_bundle(enriched, engine=engine, apply=True, allow_partial=True)['updated'] == 4
        # Replaying a smaller selection of the SAME filing retains known cash.
        subset = deepcopy(document)
        for row in subset['records']:
            row['source']['observed_at'] = '2026-09-23T00:00:00+00:00'
        load_bundle(subset, engine=engine, apply=True, allow_partial=True)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM dbo.CompanyFundamental WHERE CashAndEquivalents=123')).scalar_one() == 4
        # A NEW filing cannot inherit absent metrics from the old snapshot.
        restated = deepcopy(document)
        for row in restated['records']:
            row['source']['published_at'] = '2026-09-24T00:00:00+00:00'
            row['source']['notification_id'] = str(int(row['source']['notification_id']) + 1000000)
        assert load_bundle(restated, engine=engine, apply=True, allow_partial=True)['updated'] == 4
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM dbo.CompanyFundamental WHERE CashAndEquivalents IS NULL')).scalar_one() == 4
    finally:
        engine.dispose()
