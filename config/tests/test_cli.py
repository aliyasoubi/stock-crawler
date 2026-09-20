from stock_crawler.main import main, summary_exit_code
from stock_crawler.storage import RunSummary
from stock_crawler.config import Settings


def test_failed_partial_run_has_nonzero_exit(tmp_path):
    summary = RunSummary(tmp_path, 'sync')
    summary.add_company({'ticker': 'THYAO', 'status': 'published'})
    summary.add_company({'ticker': 'INVALID', 'status': 'unresolved'})
    assert summary_exit_code(summary) == 1
    summary.data['stopped_reason'] = 'budget'
    assert summary_exit_code(summary) == 2


def test_bad_configuration_has_concise_nonzero_error(monkeypatch, capsys):
    monkeypatch.setenv('REQUEST_DELAY_MIN_SECONDS', '-1')
    assert main(['sync']) == 1
    assert 'error:' in capsys.readouterr().err


def test_odbc_password_cannot_inject_connection_options():
    settings = Settings(_env_file=None, mssql_password='hello;}Encrypt=no')
    value = settings.sqlalchemy_url().query['odbc_connect']
    assert 'PWD={hello;}}Encrypt=no}' in value
    assert value.endswith('Encrypt=yes;TrustServerCertificate=no')


def test_schema_in_wheel_matches_checkout():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert (root/'sql/schema.sql').read_bytes() == (root/'src/stock_crawler/sql/schema.sql').read_bytes()
