import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import warnings

from openpyxl import load_workbook
import pytest

from stock_crawler.client_export import build_export, load_company_map, target_decimal, TARGET_FIELDS, UNAVAILABLE_FIELDS
from stock_crawler.config import SettingsError
from stock_crawler.main import main

ROOT = Path(__file__).resolve().parents[1]
BOOK = ROOT / 'tests/fixtures/kap/exports/two_companies_2023_2024.xlsx'
REGISTRY = ROOT / 'config/kap_companies.json'


def export(settings, paths=None, **kwargs):
    settings.kap_company_registry = REGISTRY
    return build_export(paths or [BOOK], settings=settings, **kwargs)


def changed_book(tmp_path, change, name='changed.xlsx'):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        workbook = load_workbook(BOOK)
    change(workbook.active)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    path = tmp_path / name
    path.write_bytes(output.getvalue())
    return path


def test_real_workbook_mapping_and_missing_fields(settings):
    result = export(settings, company_map={'ASELS': 102, 'THYAO': 101})
    assert result['selected_records'] == 4
    assert result['records_passing_required_fields_and_numeric_checks'] == 0
    assert not result['errors'] and result['http_attempts'] == 0
    rows = {(r['ticker'], r['values']['FiscalYear']): r for r in result['records']}
    thyao = rows['THYAO', 2024]
    values = thyao['values']
    assert tuple(values) == TARGET_FIELDS
    assert values['CompanyId'] == 101
    assert values['Revenue'] == '745430000000.0000'
    assert values['TotalLiabilities'] == '719594000000.0000'
    assert values['NetIncome'] == '113357000000.0000'
    assert values['PublishDate'] == '2025-02-28'
    assert values['PeriodEndDate'] == '2024-12-31'
    assert values['FiscalQuarter'] == 4
    assert set(thyao['missing_required_fields']) == set(UNAVAILABLE_FIELDS)
    assert values['TotalDebtShort'] is None and values['TotalDebtLong'] is None
    assert thyao['source']['period_basis'] == 'annual_year_to_date_not_standalone_Q4'
    assert not thyao['validation_issues']


def test_client_ids_are_not_invented_and_income_basis_is_explicit(settings):
    result = export(settings, net_income_basis='owners-of-parent')
    row = next(r for r in result['records'] if r['ticker'] == 'THYAO' and r['values']['FiscalYear'] == 2024)
    assert row['values']['CompanyId'] is None
    assert 'CompanyId' in row['missing_required_fields']
    assert row['values']['NetIncome'] == '113378000000.0000'
    assert row['source']['net_income_basis'] == 'owners-of-parent'


def test_currency_mismatch_is_explicit_and_does_not_convert(settings):
    result = export(settings, currency='USD')
    assert all(any('currency_mismatch' in i for i in r['validation_issues']) for r in result['records'])
    assert all(r['source']['currency_code'] == 'TRY' for r in result['records'])


def test_same_workbook_is_deduplicated_and_scope_is_respected(settings, tmp_path):
    copy = tmp_path / 'copy.xlsx'
    copy.write_bytes(BOOK.read_bytes())
    result = export(settings, paths=[BOOK, copy, BOOK])
    assert result['selected_records'] == 4 and result['source_workbooks'] == 1
    other = export(settings, scope='unconsolidated', tickers=['THYAO'])
    assert not other['records'] and other['skipped_other_scope'] == 2
    assert other['errors'][0]['ticker'] == 'THYAO'


def test_equal_rank_conflicting_source_is_not_silently_accepted(settings, tmp_path):
    p = changed_book(tmp_path, lambda s: setattr(s['P8'], 'value', '999.000'))
    result = export(settings, paths=[BOOK, p])
    assert any('conflicting_same_version' in i for r in result['records'] for i in r['validation_issues'])
    assert result['selected_records'] == 4


def test_later_notification_takes_precedence(settings, tmp_path):
    def change(s):
        s['B8'] = '9999999'
        s['C8'] = '01-01-2026 12:00:00'
        s['P8'] = '999.000'
    p = changed_book(tmp_path, change)
    result = export(settings, paths=[BOOK, p])
    row = next(r for r in result['records'] if r['source']['notification_id'] == '9999999')
    assert row['values']['PublishDate'] == '2026-01-01'
    assert result['selected_records'] == 4


def test_formula_and_unsupported_noncalendar_rows_do_not_become_client_data(settings, tmp_path):
    p = changed_book(tmp_path, lambda s: setattr(s['P8'], 'value', '=1+1'))
    result = export(settings, paths=[p])
    assert not result['records'] and 'formula' in result['errors'][0]['error']
    settings.kap_non_calendar_year_tickers = ['THYAO']
    result = export(settings)
    assert {r['ticker'] for r in result['records']} == {'ASELS'}
    assert len(result['errors']) == 2


@pytest.mark.parametrize('value,p,s,expected,issue', [
    ('0',22,4,'0.0000',None),
    ('-123.45',22,4,'-123.4500',None),
    ('1.23455',22,4,'1.2346','rounding_required'),
    ('999999999999999999.9999',22,4,'999999999999999999.9999',None),
    ('999999999999999999.99999',22,4,None,'out_of_range'),
    ('10000000000',14,4,None,'out_of_range'),
    ('100000000000000000000',22,2,None,'out_of_range'),
    ('NaN',22,4,None,'non_finite'),
])
def test_sql_decimal_limits_and_rounding(value,p,s,expected,issue):
    assert target_decimal(Decimal(value),p,s)==(expected,issue)


@pytest.mark.parametrize('contents', [
    '{"ASELS": true}', '{"ASELS": 2147483648}', '{"ASELS": 0}',
    '{"ASELS": 1, "THYAO": 1}', '{"ASELS": 1, "ASELS": 2}',
    '[]', '{"asels": 2}',
])
def test_invalid_company_maps_are_rejected(tmp_path, contents):
    p=tmp_path/'map.json'; p.write_text(contents)
    with pytest.raises(SettingsError): load_company_map(p)


def test_cli_reads_saved_exports_without_db_or_http(monkeypatch, tmp_path):
    import stock_crawler.main as cli
    import httpx
    def forbidden(*args, **kwargs):
        raise AssertionError('review export must not contact SQL or HTTP')
    monkeypatch.setattr(cli, 'make_engine', forbidden)
    monkeypatch.setattr(httpx.Client, 'request', forbidden)
    monkeypatch.setenv('KAP_COMPANY_REGISTRY',str(REGISTRY))
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    raw=tmp_path/'raw/_exports/example'; raw.mkdir(parents=True)
    (raw/'source.xlsx').write_bytes(BOOK.read_bytes())
    output=tmp_path/'client.json'
    assert main(['export-company-fundamentals','--currency','TRY','--output',str(output)])==1
    result=json.loads(output.read_text())
    assert result['selected_records']==4
    assert result['missing_required_field_counts']['Ebitda']==4
    assert main(['export-company-fundamentals','--input',str(BOOK),'--currency','TRY','--output',str(BOOK)])==1
