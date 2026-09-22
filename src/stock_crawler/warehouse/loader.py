"""Explicit warehouse contracts, immutable input capture and review-first SQL loading.

The target tables are owned by the client. We never create or alter them. FactorStore
is deliberately absent from the allowlist. SQL values are always bound parameters.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import re

from ..crawl.client_export import TARGET_FIELDS, NULLABLE_FIELDS, target_decimal
from ..core.storage import dump_json, sha256_bytes, utcnow, write_atomic

CONTRACTS = {
    'Market': ('MarketId MarketCode CountryCode CountryName BaseCurrency', ('MarketId',)),
    'Company': ('CompanyId Ticker MarketId FullName SectorName ReportingCurrency IsActive IpoDate', ('CompanyId',)),
    'CompanyFundamental': (' '.join(TARGET_FIELDS), ('CompanyId', 'FiscalYear', 'FiscalQuarter')),
    'MarketData': ('TradeDate CompanyId OpenPrice HighPrice LowPrice ClosePrice Volume ValueTraded SourcePriority', ('TradeDate', 'CompanyId')),
    'MarketIndexMaster': ('IndexId IndexCode MarketId IndexName', ('IndexId',)),
    'MarketIndexData': ('TradeDate IndexId ClosePrice', ('TradeDate', 'IndexId')),
    'MacroSovereign': ('MarketId AsOfDate PeriodType PublishDate Gdp TaxRevenue PublicDebt InterestRate Cpi FxRateUsd CdsSpreadBps', ('MarketId', 'AsOfDate', 'PeriodType')),
}
KEYS = {t: c[1] for t, c in CONTRACTS.items()}
COLUMNS = {t: c[0].split() for t, c in CONTRACTS.items()}
TEXT_LENGTHS = {'MarketCode': 10, 'CountryCode': 2, 'CountryName': 50, 'BaseCurrency': 3,
                'Ticker': 32, 'FullName': 255, 'SectorName': 100, 'ReportingCurrency': 3,
                'IndexCode': 20, 'IndexName': 100, 'PeriodType': 10}
INTS = {'MarketId': 2147483647, 'CompanyId': 2147483647, 'IndexId': 2147483647,
        'FiscalYear': 32767, 'FiscalQuarter': 4, 'Volume': 9223372036854775807,
        'SourcePriority': 255, 'IsActive': 1}
DATES = {'TradeDate', 'AsOfDate', 'PublishDate', 'PeriodEndDate', 'IpoDate'}
# The only MarketData column allowed to be absent. İş Yatırım's historical feed reports no
# opening price at all; the daily snapshot does. A backfilled row is therefore complete
# except for OpenPrice, and rejecting it would discard every pre-adapter trading day.
MARKETDATA_NULLABLE = frozenset({'OpenPrice'})
DECIMALS = {f: (22, 4) for f in TARGET_FIELDS[5:]}
DECIMALS.update(Eps=(14, 4), SharesOutstanding=(22, 2), OpenPrice=(18, 4), HighPrice=(18, 4),
                LowPrice=(18, 4), ClosePrice=(18, 4), ValueTraded=(24, 4), Gdp=(24, 4),
                TaxRevenue=(24, 4), PublicDebt=(24, 4), InterestRate=(8, 4), Cpi=(10, 4),
                FxRateUsd=(14, 6), CdsSpreadBps=(10, 2))


def validate_record(record, *, allow_partial=False):
    table, values = record['table'], record['values']
    if table not in CONTRACTS:
        raise ValueError('table is not in warehouse allowlist')
    if set(values) - set(COLUMNS[table]):
        raise ValueError(f'unknown columns for {table}: {set(values) - set(COLUMNS[table])}')
    issues = list(record.get('validation_issues', []))
    normalized = {}
    for key, value in values.items():
        if value is None or value == '':
            normalized[key] = None
            continue
        if key in INTS:
            if isinstance(value, bool) and key != 'IsActive':
                raise ValueError(f'{key}: boolean is not an integer ID')
            if not re.fullmatch(r'\d+', str(int(value)) if isinstance(value, bool) else str(value)):
                raise ValueError(f'{key}: integer required')
            value = int(value)
            low = 0 if key in ('Volume', 'IsActive') else 1
            if not low <= value <= INTS[key]:
                raise ValueError(f'{key}: out of range')
        elif key in DATES:
            value = date.fromisoformat(str(value)).isoformat()
        elif key in DECIMALS:
            value, issue = target_decimal(Decimal(str(value)), *DECIMALS[key])
            if issue:
                issues.append(f'{key}: {issue}')
        elif key in TEXT_LENGTHS:
            value = str(value)
            if len(value) > TEXT_LENGTHS[key]:
                raise ValueError(f'{key}: exceeds SQL column length')
            if key in ('BaseCurrency', 'ReportingCurrency') and not re.fullmatch('[A-Z]{3}', value):
                raise ValueError('currency requires three uppercase letters')
        normalized[key] = value
    required = set(KEYS[table])
    if table in ('Market', 'MarketIndexMaster'):
        required |= set(COLUMNS[table])
    if table == 'Company':
        required |= {'Ticker', 'MarketId', 'FullName'}
    if table == 'CompanyFundamental':
        required |= set(TARGET_FIELDS[:5]) if allow_partial else set(TARGET_FIELDS) - NULLABLE_FIELDS
        if allow_partial and not any(normalized.get(f) is not None for f in TARGET_FIELDS[5:]):
            issues.append('fundamental row contains no observation')
    if table == 'MarketData':
        required |= set(COLUMNS[table]) - MARKETDATA_NULLABLE
        if record.get('source', {}).get('price_basis') != 'as_traded':
            issues.append('verified as_traded price basis required')
        priority = record.get('source', {}).get('source_priority')
        if priority != normalized.get('SourcePriority'):
            issues.append('SourcePriority must match reviewed source metadata source_priority')
        if all(normalized.get(f) is not None for f in ('HighPrice', 'LowPrice', 'ClosePrice')):
            h, l, c = (Decimal(normalized[f]) for f in ('HighPrice', 'LowPrice', 'ClosePrice'))
            # An absent open still leaves low <= close <= high checkable; never skip the range test.
            traded = [c] if normalized.get('OpenPrice') is None else [c, Decimal(normalized['OpenPrice'])]
            if not 0 < l <= min(traded) <= max(traded) <= h:
                issues.append('invalid OHLC range')
        if normalized.get('ValueTraded') is not None and Decimal(normalized['ValueTraded']) < 0:
            issues.append('ValueTraded must be nonnegative')
    if table == 'MarketIndexData':
        required.add('ClosePrice')
        if normalized.get('ClosePrice') is not None and Decimal(normalized['ClosePrice']) <= 0:
            issues.append('index close must be positive')
    if table == 'MacroSovereign':
        if normalized.get('PeriodType') not in ('D', 'M', 'Q', 'A'):
            issues.append('PeriodType must be D/M/Q/A')
        metric_fields = set(COLUMNS[table]) - set(KEYS[table]) - {'PublishDate'}
        if not any(normalized.get(f) is not None for f in metric_fields):
            issues.append('macro row contains no observation')
        if not record.get('source', {}).get('series_metadata'):
            issues.append('macro series definition, frequency and units metadata required')
    if table == 'CompanyFundamental':
        source = record.get('source', {})
        if source.get('period_basis') != 'YTD' or not source.get('currency') or not source.get('scope'):
            issues.append('fundamental currency, scope and YTD provenance required')
        if not source.get('published_at') or not source.get('notification_id'):
            issues.append('fundamental publication timestamp and notification ID required')
    source = record.get('source', {})
    if not source.get('provider') or not source.get('observed_at'):
        issues.append('source provider and observed_at required')
    else:
        observed = datetime.fromisoformat(source['observed_at'])
        if observed.tzinfo is None:
            issues.append('observed_at timezone required')
    result = dict(record, values=normalized, validation_issues=sorted(set(issues)),
                  missing_required_fields=sorted(f for f in required if normalized.get(f) is None))
    return result


def archive_file(path, root, provider):
    data = Path(path).read_bytes()
    digest = sha256_bytes(data)
    if not re.fullmatch('[a-z0-9_-]+', provider):
        raise ValueError('invalid provider name')
    write_atomic(Path(root) / 'raw' / provider / digest / ('source' + Path(path).suffix), data)
    return digest


def import_csv(path, table, *, source, archive_dir):
    if table not in CONTRACTS or table == 'CompanyFundamental':
        raise ValueError('use warehouse-fundamentals for CompanyFundamental')
    digest = archive_file(path, archive_dir, source['provider'])
    source = dict(source, raw_sha256=digest, parser_version='warehouse-csv-1.0.0')
    source.setdefault('observed_at', utcnow().isoformat())
    data = Path(path).read_text('utf-8-sig')
    reader = csv.DictReader(StringIO(data))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError('missing/duplicate CSV headers')
    if set(reader.fieldnames) - set(COLUMNS[table]):
        raise ValueError('CSV headers must be exact target column names; normalize vendor format first')
    records, seen = [], {}
    for line, row in enumerate(reader, 2):
        if None in row or any(v is None for v in row.values()):
            raise ValueError(f'CSV row width mismatch at line {line}')
        record = validate_record({'table': table, 'values': row, 'source': dict(source, row_number=line)})
        key = tuple(record['values'].get(k) for k in KEYS[table])
        if key in seen:
            if seen[key] != record['values']:
                raise ValueError(f'conflicting duplicate key at line {line}')
            continue
        seen[key] = record['values']
        records.append(record)
    return bundle(records)


def bundle(records, errors=None, *, allow_partial=False):
    records = [validate_record(r, allow_partial=allow_partial) for r in records]
    return {'schema_version': 1, 'kind': 'warehouse_bundle', 'records': records, 'errors': errors or [],
            'summary': {'records': len(records), 'ready': sum(not r['missing_required_fields'] and not r['validation_issues'] for r in records)}}


def should_replace(table, incoming, existing, prior_source=None):
    """Lower source priority wins; older vintages never overwrite newer data.

    Existing rows without our audit history are protected, except strictly better
    MarketData source priority. Resolve that one-time ownership issue explicitly.
    """
    if existing is None:
        return True
    if table == 'MarketData':
        new, old = incoming['values']['SourcePriority'], existing.get('SourcePriority')
        if old is not None and new != old:
            return new < old
    if prior_source is None:
        if table in ('Market', 'Company', 'MarketIndexMaster'):
            # Master rows seeded outside the loader may be enriched, but existing
            # known facts must agree. No implicit takeover or ticker reassignment.
            values = incoming['values']
            return (any(existing.get(k) is None and v is not None for k, v in values.items())
                    and all(v is None or existing.get(k) is None or str(existing[k]) == str(v)
                            or (k == 'IsActive' and int(existing[k]) == int(v))
                            for k, v in values.items()))
        return False
    new = incoming['source']
    if table == 'MacroSovereign':
        old_series, new_series = prior_source.get('series_metadata', {}), new.get('series_metadata', {})
        for field in old_series.keys() & new_series.keys():
            if any(old_series[field].get(k) != new_series[field].get(k)
                   for k in ('code', 'frequency', 'unit', 'definition', 'scale')):
                return False
    if table in ('MarketData', 'MarketIndexData'):
        if new.get('currency') != prior_source.get('currency') or new.get('price_basis') != prior_source.get('price_basis'):
            return False
        if table == 'MarketIndexData' and new.get('source_priority') is not None and prior_source.get('source_priority') is not None:
            if new['source_priority'] != prior_source['source_priority']:
                return new['source_priority'] < prior_source['source_priority']
    if table == 'CompanyFundamental':
        if (new['currency'] != prior_source.get('currency') or new['scope'] != prior_source.get('scope')
                or new.get('net_income_basis') != prior_source.get('net_income_basis')):
            return False
        rank = lambda s: (datetime.fromisoformat(s['published_at']), int(s['notification_id']))
        if rank(new) != rank(prior_source):
            return rank(new) > rank(prior_source)
        # Same filing may be enriched, but non-null conflicts require review.
        return all(existing.get(k) is None or str(existing[k]) == str(v) or
                   (k in DECIMALS and v is not None and Decimal(str(existing[k])) == Decimal(str(v)))
                   for k, v in incoming['values'].items() if v is not None)
    return datetime.fromisoformat(new['observed_at']) > datetime.fromisoformat(prior_source['observed_at'])


def load_bundle(document, *, engine=None, apply=False, allow_partial=False):
    """Dry-run without SQL; --apply requires installed staging DDL and client tables.

    One transaction, serializable isolation, audit append, immutable batch payload.
    Missing observations are staged; target constraints are rechecked by reflection.
    """
    if document.get('kind') != 'warehouse_bundle' or document.get('schema_version') != 1:
        raise ValueError('unsupported warehouse bundle')
    report = bundle(document['records'], document.get('errors'), allow_partial=allow_partial)
    partial = sum(r['table'] == 'CompanyFundamental' and
                  any(r['values'].get(f) is None for f in TARGET_FIELDS[5:]) for r in report['records'])
    seen = set()
    for record in report['records']:
        key = (record['table'], *(record['values'].get(k) for k in KEYS[record['table']]))
        if all(v is not None for v in key):
            if key in seen:
                raise ValueError('duplicate target key in bundle; resolve candidates before loading')
            seen.add(key)
    if not apply:
        return dict(report['summary'], mode='dry_run_no_sql', source_errors=len(report['errors']),
                    partial_fundamentals=partial, allow_partial=allow_partial)
    if report['errors']:
        raise ValueError('resolve source errors before loading this batch')
    if not report['records']:
        raise ValueError('empty batch')
    from sqlalchemy import MetaData, Table, select, insert, update, text
    if engine.dialect.name != 'mssql':
        raise ValueError('warehouse apply supports SQL Server only')
    serialized = dump_json(report)
    batch_hash = sha256_bytes(serialized.encode())
    counts = {'staged': 0, 'inserted': 0, 'updated': 0, 'skipped': 0, 'incomplete': 0}
    with engine.connect().execution_options(isolation_level='SERIALIZABLE') as conn:
        with conn.begin():
            # Serialize this loader across processes; negative return codes must abort.
            lock = conn.execute(text("DECLARE @r int; EXEC @r = sys.sp_getapplock @Resource=N'stock-crawler-warehouse', @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=10000; SELECT @r;")).scalar_one()
            if lock < 0:
                raise ValueError('could not acquire warehouse load lock')
            md = MetaData()
            batches = Table('WarehouseBatch', md, schema='stg', autoload_with=conn)
            observations = Table('WarehouseObservation', md, schema='stg', autoload_with=conn)
            if conn.execute(select(batches.c.BatchHash).where(batches.c.BatchHash == batch_hash)).first():
                for status in conn.execute(select(observations.c.Status).where(observations.c.BatchHash == batch_hash)).scalars():
                    counts[status] += 1
                    counts['staged'] += 1
                return dict(counts, mode='already_loaded', batch_hash=batch_hash)
            conn.execute(insert(batches).values(BatchHash=batch_hash, CapturedAt=utcnow(), Payload=serialized))
            target_tables = {}
            for record in report['records']:
                name, values = record['table'], record['values']
                key = {k: values.get(k) for k in KEYS[name]}
                key_json = dump_json(key)
                key_hash = sha256_bytes(key_json.encode())
                status = 'incomplete'
                reason = None
                if not record['missing_required_fields'] and not record['validation_issues']:
                    target = target_tables.get(name)
                    if target is None:
                        target = Table(name, md, schema='dbo', autoload_with=conn)
                        target_tables[name] = target
                    if set(values) - set(target.c.keys()):
                        raise ValueError(f'{name}: target is missing expected columns')
                    if set(c.name for c in target.primary_key) != set(KEYS[name]):
                        raise ValueError(f'{name}: target primary key does not match contract')
                    # Existing schema precision/scale must match the published contract.
                    for field in values:
                        column = target.c[field]
                        if field in DECIMALS and (getattr(column.type, 'precision', None), getattr(column.type, 'scale', None)) != DECIMALS[field]:
                            raise ValueError(f'{name}.{field}: SQL precision/scale differs from contract')
                    typed = {k: date.fromisoformat(v) if k in DATES and v is not None else
                             Decimal(v) if k in DECIMALS and v is not None else v for k, v in values.items()}
                    where = [target.c[k] == typed[k] for k in KEYS[name]]
                    existing = conn.execute(select(target).where(*where)).mappings().first()
                    if name == 'CompanyFundamental':
                        reporting_currency = conn.execute(text('SELECT ReportingCurrency FROM dbo.Company WHERE CompanyId=:id'),
                                                          {'id': typed['CompanyId']}).scalar()
                        if reporting_currency and reporting_currency != record['source']['currency']:
                            raise ValueError('CompanyFundamental currency differs from Company.ReportingCurrency')
                    prior = conn.execute(select(observations.c.SourceJson).where(
                        observations.c.TargetTable == name, observations.c.KeyHash == key_hash,
                        observations.c.Status.in_(['inserted', 'updated'])).order_by(observations.c.ObservationId.desc())).scalar()
                    prior = json.loads(prior) if prior else None
                    # Single PublishDate cannot represent mixed macro release dates.
                    # Keep release dates per field in the observation history instead.
                    if name == 'MacroSovereign' and existing is not None:
                        old_metrics = {k for k in DECIMALS if k in existing and existing[k] is not None}
                        new_metrics = {k for k in DECIMALS if typed.get(k) is not None}
                        if old_metrics - new_metrics and existing.get('PublishDate') != typed.get('PublishDate'):
                            typed['PublishDate'] = None
                    if existing is None:
                        absent = [c.name for c in target.c if not c.nullable and typed.get(c.name) is None
                                  and not c.identity and c.server_default is None and not c.computed]
                        if absent:
                            reason = 'SQL NOT NULL columns absent: ' + ', '.join(absent)
                        elif any(target.c[k].identity for k in KEYS[name]):
                            reason = 'explicit IDs supplied to IDENTITY table; seed master rows in client DB and export IDs first'
                        else:
                            conn.execute(insert(target).values(**typed))
                            status = 'inserted'
                    elif should_replace(name, record, existing, prior):
                        # Never erase existing facts when a new feed omits a field.
                        changes = {k: v for k, v in typed.items() if k not in KEYS[name] and v is not None}
                        if name == 'CompanyFundamental':
                            # A restated filing is a whole financial snapshot, never a mix
                            # of old optional balances and newly published mandatory fields.
                            same_filing = prior and all(record['source'].get(k) == prior.get(k)
                                                        for k in ('notification_id', 'published_at'))
                            if not same_filing:
                                changes = {k: typed.get(k) for k in COLUMNS[name] if k not in KEYS[name]}
                            invalid_nulls = [k for k, v in changes.items() if v is None and not target.c[k].nullable]
                            if invalid_nulls:
                                raise ValueError(f'{name}: SQL NOT NULL conflict for {invalid_nulls}')
                        if name == 'MacroSovereign' and typed.get('PublishDate') is None:
                            if not target.c.PublishDate.nullable:
                                raise ValueError('MacroSovereign.PublishDate must allow NULL for unknown/mixed release dates')
                            changes['PublishDate'] = None
                        if changes:
                            conn.execute(update(target).where(*where).values(**changes))
                            status = 'updated'
                        else:
                            status = 'skipped'
                    else:
                        status, reason = 'skipped', 'existing row outranks candidate or has no loader provenance'
                audit_source = record['source']
                if name == 'CompanyFundamental' and status == 'updated' and prior and same_filing:
                    fields = dict(prior.get('fields', {}))
                    fields.update({k: v for k, v in audit_source.get('fields', {}).items() if values.get(k) is not None})
                    audit_source = dict(audit_source, fields=fields,
                        workbook_sha256=sorted(set(prior.get('workbook_sha256', []) + audit_source.get('workbook_sha256', []))))
                if name == 'MacroSovereign' and status == 'updated' and prior:
                    audit_source = dict(audit_source, series_metadata={**prior.get('series_metadata', {}), **audit_source.get('series_metadata', {})})
                conn.execute(insert(observations).values(BatchHash=batch_hash, TargetTable=name,
                    KeyHash=key_hash, KeyJson=key_json, SourceJson=dump_json(audit_source),
                    ValuesJson=dump_json(values), Status=status,
                    IssuesJson=dump_json({'missing': record['missing_required_fields'], 'issues': record['validation_issues'], 'reason': reason})))
                counts['staged'] += 1
                counts[status] += 1
    return dict(counts, mode='applied', batch_hash=batch_hash)
