"""Small SQL Server setup/reference helpers; no network acquisition or scheduler."""
from __future__ import annotations

import csv
from importlib.resources import files
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import MetaData, Table, func, insert, inspect, select, text

from ..core.config import Settings
from ..core.db import split_batches
from ..core.storage import dump_json, write_atomic
from .loader import BIST_MARKET, COLUMNS, INDEX_NAMES, KEYS


def warehouse_settings(path=None, *, use_bootstrap=False):
    """Resolve local Docker settings, or an explicit external warehouse file.

    With no path, reuse the normal application environment injected by Docker
    Compose.  Only ``init-db`` requests the bootstrap login; ordinary warehouse
    reads/writes use the same least-privilege writer as ``stock-crawler sync``.
    An explicit file remains authoritative for a separate client database.
    """
    if path is None:
        settings = Settings()
        if use_bootstrap:
            user = settings.mssql_bootstrap_user
            password = settings.mssql_bootstrap_password.get_secret_value()
            if not user or not password:
                raise ValueError('MSSQL_BOOTSTRAP_USER and MSSQL_BOOTSTRAP_PASSWORD are required for warehouse init-db')
            return settings.model_copy(update={
                'mssql_user': user,
                'mssql_password': settings.mssql_bootstrap_password,
            })
        if not settings.mssql_user or not settings.mssql_password.get_secret_value():
            raise ValueError('MSSQL_USER and MSSQL_PASSWORD are required for warehouse database access')
        return settings
    if not Path(path).is_file():
        raise ValueError('the selected warehouse --env-file does not exist')
    raw = {k.lower(): v for k, v in dotenv_values(path, interpolate=False).items()}
    required = ('mssql_host', 'mssql_port', 'mssql_database', 'mssql_user', 'mssql_password')
    if any(not raw.get(k) for k in required):
        raise ValueError('warehouse env file must explicitly define MSSQL_HOST, MSSQL_PORT, MSSQL_DATABASE, MSSQL_USER and MSSQL_PASSWORD')
    # Explicit init values outrank process env. Use declared defaults for optional
    # MSSQL settings, never credentials inherited from the annual crawler.
    values = {k: raw.get(k, field.default) for k, field in Settings.model_fields.items() if k.startswith('mssql_')}
    return Settings(_env_file=None, **values)


def require_mssql(engine):
    if engine.dialect.name != 'mssql':
        raise ValueError('warehouse administration supports SQL Server only')


def lock(conn):
    result = conn.execute(text("DECLARE @r int; EXEC @r = sys.sp_getapplock @Resource=N'stock-crawler-warehouse', @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=10000; SELECT @r;")).scalar_one()
    if result < 0:
        raise ValueError('could not acquire warehouse lock')


def initialize(engine):
    """Create missing tables in an EXISTING database; never alter existing targets."""
    require_mssql(engine)
    with engine.begin() as conn:
        lock(conn)
        database = conn.execute(text('SELECT DB_NAME()')).scalar_one()
        for name in ('warehouse-schema.sql', 'warehouse-staging.sql', 'warehouse-permissions.sql'):
            for batch in split_batches(files('stock_crawler').joinpath('sql', name).read_text('utf-8')):
                conn.execute(text(batch))
    return {'mode': 'initialized', 'database': database,
            'note': 'Missing tables created; existing columns unchanged. Run inspect for actual database/schema.'}


def insert_master(conn, table, values, natural_keys):
    matches = conn.execute(select(table).where(*(table.c[k] == values[k] for k in natural_keys))).mappings().all()
    if len(matches) > 1:
        raise ValueError(f'{table.name}: duplicate natural key; reconcile existing master data')
    if matches:
        if table.name == 'Market' and any(matches[0][k] != v for k, v in values.items()):
            raise ValueError('existing BIST market conflicts with TR/TRY seed; review it first')
        return dict(matches[0]), False
    pk = KEYS[table.name][0]
    if set(c.name for c in table.primary_key) != {pk}:
        raise ValueError(f'{table.name}: primary key differs from contract')
    values = dict(values)
    if not table.c[pk].identity:
        # Serialized bootstrap of existing non-IDENTITY master tables. These are
        # locally allocated keys, never KAP IDs, and are exported after insertion.
        values[pk] = int(conn.execute(select(func.max(table.c[pk]))).scalar() or 0) + 1
    missing = [c.name for c in table.c if not c.nullable and values.get(c.name) is None
               and not c.identity and c.server_default is None and not c.computed]
    if missing:
        raise ValueError(f'{table.name}: existing NOT NULL columns require verified values: {", ".join(missing)}')
    conn.execute(insert(table).values(**values))
    row = conn.execute(select(table).where(*(table.c[k] == values[k] for k in natural_keys))).mappings().one()
    return dict(row), True


def seed_reference(engine, companies=()):
    require_mssql(engine)
    counts = {'Market': 0, 'MarketIndexMaster': 0, 'Company': 0}
    with engine.connect().execution_options(isolation_level='SERIALIZABLE') as conn:
        with conn.begin():
            lock(conn)
            md = MetaData()
            tables = {name: Table(name, md, schema='dbo', autoload_with=conn) for name in counts}
            market, created = insert_master(conn, tables['Market'], dict(BIST_MARKET), ['MarketCode'])
            counts['Market'] += created
            for code, name in INDEX_NAMES.items():
                _, created = insert_master(conn, tables['MarketIndexMaster'], {
                    'IndexCode': code, 'IndexName': name, 'MarketId': market['MarketId']}, ['MarketId', 'IndexCode'])
                counts['MarketIndexMaster'] += created
            for entry in companies:
                _, created = insert_master(conn, tables['Company'], {
                    'Ticker': entry['ticker'], 'FullName': entry['company_name'], 'MarketId': market['MarketId']}, ['MarketId', 'Ticker'])
                counts['Company'] += created
    return {'mode': 'seeded', 'inserted': counts, 'MarketId': market['MarketId'],
            'note': 'Existing company rows preserved. Registry candidates have no inferred active status, sector, currency or IPO date.'}


def inspect_database(engine):
    require_mssql(engine)
    with engine.connect() as conn:
        inspector = inspect(conn)
        result = {'database': conn.execute(text('SELECT DB_NAME()')).scalar_one(), 'tables': {}}
        for name, columns in COLUMNS.items():
            if not inspector.has_table(name, schema='dbo'):
                result['tables'][name] = {'exists': False}
                continue
            actual = inspector.get_columns(name, schema='dbo')
            result['tables'][name] = {
                'exists': True,
                'rows': conn.execute(text(f'SELECT COUNT_BIG(*) FROM dbo.[{name}]')).scalar_one(),
                'missing_columns': sorted(set(columns) - {c['name'] for c in actual}),
                'not_null_columns': [c['name'] for c in actual if not c['nullable']],
                'primary_key': inspector.get_pk_constraint(name, schema='dbo')['constrained_columns'],
            }
        if inspector.has_table('WarehouseObservation', schema='stg'):
            result['load_status'] = [dict(r) for r in conn.execute(text(
                'SELECT TargetTable, Status, COUNT_BIG(*) AS Rows FROM stg.WarehouseObservation GROUP BY TargetTable, Status')).mappings()]
        return result


def export_maps(engine, directory):
    require_mssql(engine)
    with engine.connect() as conn:
        markets = conn.execute(text("SELECT MarketId FROM dbo.Market WHERE MarketCode='BIST' AND CountryCode='TR' AND BaseCurrency='TRY'")).scalars().all()
        if len(markets) != 1:
            raise ValueError('expected one BIST/TR/TRY market; run seed-reference first')
        market_id = markets[0]
        companies = [dict(r) for r in conn.execute(text('SELECT * FROM dbo.Company WHERE MarketId=:id ORDER BY Ticker'), {'id': market_id}).mappings()]
        indices = [dict(r) for r in conn.execute(text('SELECT IndexId, IndexCode FROM dbo.MarketIndexMaster WHERE MarketId=:id'), {'id': market_id}).mappings()]
    def mapping(rows, key, value):
        result = {r[key]: r[value] for r in rows}
        if len(result) != len(rows):
            raise ValueError(f'duplicate {key}; refusing ambiguous ID export')
        return result
    outputs = {'client_company_ids.json': dump_json(mapping(companies, 'Ticker', 'CompanyId')),
               'client_index_ids.json': dump_json(mapping(indices, 'IndexCode', 'IndexId')),
               'client_market_ids.json': dump_json({'BIST': market_id})}
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS['Company'], extrasaction='ignore')
    writer.writeheader()
    writer.writerows(dict(r, IsActive=int(r['IsActive']) if r.get('IsActive') is not None else None) for r in companies)
    outputs['company_metadata.csv'] = buffer.getvalue()
    # Do not destroy a manually enriched metadata file on the next map refresh.
    for name, content in outputs.items():
        path = Path(directory) / name
        if name.endswith('.csv') and path.exists():
            continue
        write_atomic(path, content.encode('utf-8'))
    return {'mode': 'exported', 'MarketId': market_id, 'companies': len(companies),
            'indices': len(indices), 'directory': str(directory),
            'note': 'Existing company_metadata.csv is preserved; JSON ID maps are refreshed.'}
