"""Bounded source capture and metadata-driven EVDS observations.

No vendor column names or macro series codes are silently guessed. An operator
captures and reviews a source profile once; the same profile drives replay/live runs.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse

import httpx

from ..core.storage import dump_json, sha256_bytes, utcnow, write_atomic
from .loader import bundle, archive_file

MAX_BYTES = 25 * 1024 * 1024
HOSTS = {'evds2.tcmb.gov.tr', 'evds3.tcmb.gov.tr', 'www.isyatirim.com.tr',
         'www.borsaistanbul.com', 'borsaistanbul.com', 'datastore.borsaistanbul.com', 'www.tcmb.gov.tr'}


def fetch_snapshot(url, output, *, key_env=None, client=None):
    """One request, no redirect/retry storm. Credentials stay in a header, never metadata."""
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password:
        raise ValueError('source URL must be HTTPS on an approved source host')
    if re.search(r'(?i)(key|token|password)=', url):
        raise ValueError('credentials must not appear in source URL')
    headers = {'User-Agent': 'StockCrawlerWarehouse/1.0', 'Accept': 'application/json, application/xml, text/csv, application/octet-stream'}
    if key_env:
        if parsed.hostname not in {'evds2.tcmb.gov.tr', 'evds3.tcmb.gov.tr'}:
            raise ValueError('API key header is supported only for TCMB')
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f'set {key_env} in the process environment')
        headers['key'] = key
    own = client is None
    client = client or httpx.Client(timeout=40, follow_redirects=False)
    try:
        with client.stream('GET', url, headers=headers) as response:
            if response.status_code in (429, 503):
                raise ValueError(f'source throttled ({response.status_code}); Retry-After={response.headers.get("Retry-After", "unspecified")}; stop and retry later')
            if response.status_code != 200:
                raise ValueError(f'source returned HTTP {response.status_code}; redirects are not followed')
            chunks, size = [], 0
            for part in response.iter_bytes():
                size += len(part)
                if size > MAX_BYTES:
                    raise ValueError('source exceeds 25 MiB limit')
                chunks.append(part)
            data = b''.join(chunks)
            if not data or data.lstrip()[:20].lower().startswith((b'<html', b'<!doctype')):
                raise ValueError('source returned empty data or an HTML page; inspect source profile')
            write_atomic(Path(output), data)
            metadata = {'source_url': url, 'observed_at': utcnow().isoformat(), 'raw_sha256': sha256_bytes(data),
                        'content_type': response.headers.get('Content-Type'), 'http_attempts': 1}
            write_atomic(Path(str(output) + '.metadata.json'), dump_json(metadata).encode())
            return metadata
    finally:
        if own:
            client.close()


def tcmb_fx_url(day=None):
    suffix = day.strftime('%Y%m/%d%m%Y.xml') if day else 'today.xml'
    return 'https://www.tcmb.gov.tr/kurlar/' + suffix


def import_tcmb_fx(path, *, market_id, archive_dir, expected_date=None, observed_at=None):
    """Official indicative ForexBuying, TRY per USD; file date is authoritative.

    This is not an executable market quote. No weekend fill or fake publication
    date. A saved XML can be replayed without network or an EVDS API key.
    """
    from defusedxml.ElementTree import fromstring
    data = Path(path).read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError('TCMB XML exceeds size limit')
    root = fromstring(data)
    if root.tag != 'Tarih_Date':
        raise ValueError('expected TCMB Tarih_Date XML root')
    day = datetime.strptime(root.attrib['Tarih'], '%d.%m.%Y').date()
    if expected_date and day != expected_date:
        raise ValueError('TCMB XML date differs from requested date')
    rows = [r for r in root.findall('Currency') if r.get('CurrencyCode') == 'USD']
    if len(rows) != 1:
        raise ValueError('expected exactly one USD currency record')
    unit, buying = Decimal(rows[0].findtext('Unit')), Decimal(rows[0].findtext('ForexBuying'))
    if not unit.is_finite() or unit <= 0 or not buying.is_finite() or buying <= 0:
        raise ValueError('TCMB USD unit and buying rate must be positive finite numbers')
    digest = archive_file(path, archive_dir, 'tcmb_fx')
    return bundle([{'table': 'MacroSovereign', 'values': {
        'MarketId': market_id, 'AsOfDate': day.isoformat(), 'PeriodType': 'D',
        'PublishDate': None, 'FxRateUsd': str(buying / unit)}, 'source': {
        'provider': 'tcmb_fx', 'observed_at': observed_at or utcnow().isoformat(),
        'raw_sha256': digest, 'source_url': tcmb_fx_url(day), 'parser_version': 'warehouse-tcmb-fx-1.0.0',
        'series_metadata': {'FxRateUsd': {'code': 'USD.ForexBuying', 'frequency': 'D',
            'unit': 'TRY per USD', 'scale': '1', 'definition': 'TCMB indicative foreign exchange buying rate, normalized by Unit',
            'publish_date': None}}, 'release_date_basis': 'publication timestamp not supplied by this adapter'}}])


def evds_period(value, frequency):
    value = str(value).strip()
    if frequency == 'D':
        return datetime.strptime(value, '%d-%m-%Y').date()
    if frequency == 'M' and re.fullmatch(r'\d{4}-\d{1,2}', value):
        year, month = map(int, value.split('-'))
        return date(year, month, calendar.monthrange(year, month)[1])
    if frequency == 'Q' and re.fullmatch(r'\d{4}-Q[1-4]', value):
        year, quarter = value.split('-Q')
        month = int(quarter) * 3
        return date(int(year), month, calendar.monthrange(int(year), month)[1])
    if frequency == 'A' and re.fullmatch(r'\d{4}', value):
        return date(int(value), 12, 31)
    raise ValueError(f'unsupported EVDS {frequency} date {value!r}; update profile/parser explicitly')


def validate_evds_profile(profile):
    if profile.get('verified') is not True:
        raise ValueError('verify EVDS profile against current official catalog and a real response first')
    if not profile.get('catalog_evidence'):
        raise ValueError('EVDS profile needs dated catalog evidence')
    if not profile.get('series'):
        raise ValueError('EVDS profile has no series')
    targets = set()
    for spec in profile['series']:
        if spec.get('target') not in {'Gdp', 'TaxRevenue', 'PublicDebt', 'InterestRate', 'Cpi', 'FxRateUsd', 'CdsSpreadBps'}:
            raise ValueError('invalid macro target')
        if spec['target'] in targets:
            raise ValueError('duplicate macro target')
        targets.add(spec['target'])
        if spec.get('frequency') not in ('D', 'M', 'Q', 'A') or not spec.get('unit') or not spec.get('definition'):
            raise ValueError('series frequency, unit and definition required')
        if not spec.get('code') or not spec.get('json_key') or not re.fullmatch(r'[A-Za-z0-9_.]+', spec['code']):
            raise ValueError('verified series code and response json_key required')
        scale = Decimal(str(spec.get('scale', '1')))
        if not scale.is_finite() or scale <= 0:
            raise ValueError('positive finite scale required')
    return profile


def evds_url(profile, start, end):
    validate_evds_profile(profile)
    if start > end:
        raise ValueError('start must be <= end')
    # Native frequency only: no aggregation/formula/forward-fill query parameters.
    if len({s['frequency'] for s in profile['series']}) != 1:
        raise ValueError('fetch one native frequency per profile/request')
    base = profile['base_url'].rstrip('/') + '/'
    return base + urlencode({'series': '-'.join(s['code'] for s in profile['series']),
                            'startDate': start.strftime('%d-%m-%Y'), 'endDate': end.strftime('%d-%m-%Y'), 'type': 'json'})


def import_evds(path, profile, *, market_id, archive_dir, observed_at=None):
    validate_evds_profile(profile)
    if len({s['frequency'] for s in profile['series']}) != 1:
        raise ValueError('one native frequency per EVDS file')
    document = json.loads(Path(path).read_text('utf-8-sig'), parse_float=Decimal)
    rows = document.get('items')
    if not isinstance(rows, list):
        raise ValueError('EVDS response must contain items array; login/error response rejected')
    digest = archive_file(path, archive_dir, 'tcmb_evds')
    captured = observed_at or utcnow().isoformat()
    records, seen = [], {}
    for row in rows:
        values = {'MarketId': market_id, 'PublishDate': None}
        metadata = {}
        for spec in profile['series']:
            # A missing key means API/schema drift, unlike an explicit null observation.
            if spec['json_key'] not in row:
                raise ValueError(f'missing series key {spec["json_key"]}')
            raw = row[spec['json_key']]
            if raw in (None, '', '.'):
                continue
            amount = Decimal(str(raw)) * Decimal(str(spec.get('scale', '1')))
            if not amount.is_finite():
                raise ValueError('non-finite macro observation')
            end = evds_period(row[profile.get('date_key', 'Tarih')], spec['frequency'])
            values.update(AsOfDate=end.isoformat(), PeriodType=spec['frequency'])
            values[spec['target']] = str(amount)
            metadata[spec['target']] = dict(spec, raw_value=str(raw), publish_date=None)
        if not metadata:
            continue
        key = (values['AsOfDate'], values['PeriodType'])
        if key in seen:
            if seen[key] != values:
                raise ValueError('conflicting EVDS observations for the same period')
            continue
        seen[key] = values
        records.append({'table': 'MacroSovereign', 'values': values, 'source': {
            'provider': 'tcmb_evds', 'observed_at': captured, 'raw_sha256': digest,
            'parser_version': 'warehouse-evds-1.0.0', 'series_metadata': metadata,
            'catalog_evidence': profile['catalog_evidence'],
            'release_date_basis': 'unknown; observation date and retrieval date are not release dates',
            'vintage_basis': 'latest_snapshot_not_point_in_time'}})
    return bundle(records)


def normalize_vendor_csv(path, profile, *, archive_dir):
    """Explicit vendor header mapping, including locale, instrument selection and units.

    Produces records directly so the original input hash remains attached. One
    profile per BIST report type; never guess an OHLC open from a closing price.
    """
    import csv
    from io import StringIO
    from ..core.units import parse_turkish_number
    from .loader import COLUMNS, DECIMALS, INTS
    table = profile['table']
    if table not in {'Company', 'MarketData', 'MarketIndexMaster', 'MarketIndexData'}:
        raise ValueError('unsupported vendor table')
    if profile.get('verified') is not True or not profile.get('evidence'):
        raise ValueError('verify CSV headers, units and semantics in profile first')
    source = dict(profile['source'], observed_at=profile.get('observed_at') or utcnow().isoformat())
    digest = archive_file(path, archive_dir, source['provider'])
    source.update(raw_sha256=digest, parser_version='warehouse-vendor-1.0.0', profile=profile)
    records = []
    contents = Path(path).read_text(encoding=profile.get('encoding', 'utf-8-sig'))
    if profile.get('format', 'csv') == 'json':
        document = json.loads(contents, parse_float=Decimal)
        source_rows = document
        for key in profile.get('rows_path', ['value']):
            source_rows = source_rows[key]
        if not isinstance(source_rows, list) or not source_rows or not all(isinstance(r, dict) for r in source_rows):
            raise ValueError('vendor JSON rows_path must resolve to a nonempty array of objects')
        headers = list(source_rows[0])
        if any(set(row) != set(headers) for row in source_rows):
            raise ValueError('vendor JSON row keys differ; review source schema')
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=headers, delimiter=profile.get('delimiter', ','))
        writer.writeheader()
        writer.writerows(source_rows)
        contents = buffer.getvalue()
    elif profile.get('format', 'csv') != 'csv':
        raise ValueError('vendor profile format must be csv or json')
    with StringIO(contents) as handle:
        reader = csv.DictReader(handle, delimiter=profile.get('delimiter', ','))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError('missing/duplicate vendor headers')
        expected = {s['column'] for s in profile['fields'].values() if 'column' in s} | set(profile.get('filters', {}))
        if not expected <= set(reader.fieldnames):
            raise ValueError('vendor headers changed; review profile')
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f'CSV row width mismatch at {line}')
            if any(row[k] not in v for k, v in profile.get('filters', {}).items()):
                continue
            values = {}
            for target, spec in profile['fields'].items():
                if target not in COLUMNS[table]:
                    raise ValueError('unknown vendor mapping target')
                raw = row[spec['column']] if 'column' in spec else spec.get('constant')
                if spec.get('lookup') is not None and raw not in (None, ''):
                    if raw not in spec['lookup']:
                        raise ValueError(f'unknown identity {raw!r}; update explicit lookup')
                    raw = spec['lookup'][raw]
                if raw in (None, ''):
                    values[target] = None
                elif 'date_format' in spec:
                    values[target] = datetime.strptime(str(raw), spec['date_format']).date().isoformat()
                elif target in DECIMALS or target in INTS:
                    if spec.get('number_format', 'decimal') == 'turkish':
                        number = parse_turkish_number(str(raw))
                    elif spec.get('number_format') == 'english_grouped':
                        if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', str(raw)):
                            raise ValueError('invalid English grouped number')
                        number = Decimal(str(raw).replace(',', ''))
                    else:
                        number = Decimal(str(raw))
                    if number is None:
                        values[target] = None
                        continue
                    number *= Decimal(str(spec.get('scale', '1')))
                    if target in INTS:
                        if number != number.to_integral_value():
                            raise ValueError('nonintegral integer field')
                        values[target] = int(number)
                    else:
                        values[target] = str(number)
                else:
                    values[target] = raw
            records.append({'table': table, 'values': values, 'source': dict(source, row_number=line)})
    # Conflicting same-key rows (sessions/instruments) must not overwrite each other.
    from .loader import KEYS
    unique = {}
    for record in records:
        key = tuple(record['values'].get(k) for k in KEYS[table])
        if key in unique and unique[key]['values'] != record['values']:
            raise ValueError('conflicting vendor rows: filter instrument/session or aggregate by a reviewed rule')
        unique[key] = record
    return bundle(list(unique.values()))
