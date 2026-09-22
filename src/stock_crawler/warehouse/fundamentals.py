"""Quarterly client adapter. Does not change the legacy annual publication pipeline."""
from __future__ import annotations

import calendar
from collections import Counter
from datetime import date
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from ..crawl.client_export import TARGET_FIELDS, NULLABLE_FIELDS, target_decimal
from ..crawl.kap_export import CompanyRegistry, FIELDS, META_HEADERS, read_export
from ..core.storage import dump_json, sha256_bytes, utcnow, write_atomic
from ..core.units import decode_presentation_currency, parse_source_timestamp, parse_structured_number, parse_turkish_number

VERSION = 'warehouse-kap-1.0.0'
# Observed headers only. New item IDs must come from a captured KAP request.
OBSERVED = {
    'Revenue': 'Revenue', 'Total Assets': 'TotalAssets', 'Total Equity': 'Equity',
    'Current Liabilities': 'CurrentLiabilities', 'Non-current Liabilities': 'NonCurrentLiabilities',
    'Total Liabilities': 'TotalLiabilities',
    'Profit (Loss) From Operating Activities': 'OperatingIncome',
    'Net Profit (Loss)': 'NetIncome',
}
EXTRA = {'Total Liabilities', 'Gross Profit (Loss)', 'Profit (Loss) From Operating Activities', 'Cost of Sales'}
INPUTS = {'OperatingCashFlow', 'CapexCashOutflow', 'OperatingDepreciationAmortization'}


def load_mapping(path=None):
    """Reviewed extension file: exact header -> target/unit/item_id/evidence.

    EPS and shares have independent scales. Capital and net income/EPS are never shares.
    """
    if path is None:
        return {}
    mapping = json.loads(Path(path).read_text('utf-8'))
    if not isinstance(mapping, dict):
        raise ValueError('item mapping must be an object')
    targets = set(OBSERVED.values())
    for header, spec in mapping.items():
        target = spec.get('target')
        if header in (*META_HEADERS, *FIELDS, *EXTRA) or target in targets:
            raise ValueError('extension may not override an observed header or target')
        if target not in set(TARGET_FIELDS[5:]) | INPUTS:
            raise ValueError(f'unsupported mapping target {target}')
        expected = 'per_share' if target == 'Eps' else 'shares' if target == 'SharesOutstanding' else 'money'
        if spec.get('unit') != expected or not spec.get('evidence') or not spec.get('item_id'):
            raise ValueError(f'{header}: unit, captured item_id and evidence are required')
        if any('REPLACE' in str(x) for x in (header, spec['item_id'], spec['evidence'])):
            raise ValueError('replace all concept mapping placeholders with captured evidence')
        if expected != 'money':
            if parse_structured_number(spec.get('scale')) is None or Decimal(str(spec['scale'])) <= 0:
                raise ValueError(f'{header}: explicit positive unit scale required')
        if target in {'Eps', 'SharesOutstanding', 'TotalDebtShort', 'TotalDebtLong', 'Ebitda', 'FreeCashFlow'} and not spec.get('definition'):
            raise ValueError(f'{header}: reviewed definition is required')
        targets.add(target)
    return mapping


def number(raw):
    value = parse_turkish_number(raw) if isinstance(raw, str) else parse_structured_number(raw)
    if value is not None and not value.is_finite():
        raise ValueError('non-finite financial value')
    return value


def build_fundamentals(paths, *, registry_path, company_map=None, currency='TRY',
                       calendar_tickers=(), mapping=None, net_income_basis='total-profit',
                       scope='consolidated-else-unconsolidated', archive_dir=None, aliases_path=None):
    registry = CompanyRegistry(Path(registry_path), aliases_path)
    if not re.fullmatch('[A-Z]{3}', currency):
        raise ValueError('currency must be a three-letter uppercase ISO code')
    mapping = mapping or {}
    if scope not in ('consolidated-else-unconsolidated', 'consolidated', 'unconsolidated'):
        raise ValueError('invalid scope')
    if net_income_basis not in ('total-profit', 'owners-of-parent'):
        raise ValueError('invalid NetIncome basis')
    groups, errors, seen = {}, [], set()
    captured = utcnow().isoformat()
    for path in sorted(map(Path, paths)):
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('workbook exceeds 20 MiB')
        data = path.read_bytes()
        digest = sha256_bytes(data)
        if digest in seen:
            continue
        seen.add(digest)
        if archive_dir:
            write_atomic(Path(archive_dir) / 'raw' / 'kap_compare' / digest / 'source.xlsx', data)
        try:
            rows = read_export(data, extra_headers=EXTRA | set(mapping))
        except Exception as exc:
            errors.append({'file': path.name, 'error': str(exc)})
            continue
        for row in rows:
            try:
                identity = registry.match(row['Company'])
                key = (identity.ticker, str(row['Notification ID']))
                group = groups.setdefault(key, {'row': {}, 'hashes': [], 'issues': [], 'field_hashes': {}})
                if digest not in group['hashes']:
                    group['hashes'].append(digest)
                for header, value in row.items():
                    previous = group['row'].get(header)
                    if previous is not None and value is not None:
                        equal = previous == value if header in META_HEADERS else number(previous) == number(value)
                        if not equal:
                            group['issues'].append(f'conflicting_same_notification: {header}')
                    if value is not None:
                        group['row'][header] = value
                        group['field_hashes'].setdefault(header, []).append(digest)
            except Exception as exc:
                errors.append({'file': path.name, 'notification_id': row.get('Notification ID'), 'error': str(exc)})
    selected = {}
    for (ticker, notification), group in groups.items():
        row = group['row']
        try:
            quarter, year = int(row['Period']), int(row['Year'])
            if not 1 <= quarter <= 4 or not 2000 <= year <= 2100:
                raise ValueError('invalid fiscal year/period')
            statement = row['Sectoral Statement Type'].lower()
            if statement not in ('general', 'holding'):
                raise ValueError('bank/insurance/other sector taxonomy requires separate reviewed mapping')
            row_scope = row['Nature of Financial Statement'].lower()
            if row_scope not in ('consolidated', 'unconsolidated'):
                raise ValueError('unknown scope')
            if scope != 'consolidated-else-unconsolidated' and row_scope != scope:
                continue
            code, scale = decode_presentation_currency(row['Presentation Currency'])
            published = parse_source_timestamp(row['Publish Date'])
            end = date(year, quarter * 3, calendar.monthrange(year, quarter * 3)[1])
            values = dict.fromkeys(TARGET_FIELDS)
            values.update(CompanyId=(company_map or {}).get(ticker), FiscalYear=year,
                          FiscalQuarter=quarter, PeriodEndDate=end.isoformat(), PublishDate=published.astimezone(ZoneInfo('Europe/Istanbul')).date().isoformat())
            issues, fields, amounts = list(set(group['issues'])), {}, {}
            if ticker not in calendar_tickers:
                issues.append('calendar_year_not_confirmed: supply --calendar-tickers after verification')
            if code != currency:
                issues.append(f'currency_mismatch: {code} vs {currency}; no conversion')
            direct = dict(OBSERVED)
            if net_income_basis == 'owners-of-parent':
                direct.pop('Net Profit (Loss)')
                direct['Profit (Loss) Attributable To, Owners of Parent'] = 'NetIncome'
            specs = {h: {'target': t, 'unit': 'money'} for h, t in direct.items()}
            specs.update(mapping)
            with localcontext() as ctx:
                ctx.prec = 80
                for header, spec in specs.items():
                    target, raw = spec['target'], row.get(header)
                    amount = number(raw)
                    multiplier = Decimal(scale) if spec['unit'] == 'money' else Decimal(str(spec['scale']))
                    amounts[target] = None if amount is None else amount * multiplier
                    fields[target] = {'header': header, 'raw_value': raw, 'scale': str(multiplier),
                                      'workbook_sha256': sorted(set(group['field_hashes'].get(header, []))),
                                      'method': 'reported', 'definition': spec.get('definition'),
                                      'item_id': spec.get('item_id'), 'evidence': spec.get('evidence')}
                if amounts.get('Revenue') is None and row.get('Revenue from Finance Sector Operations') is not None:
                    amounts['Revenue'] = number(row['Revenue from Finance Sector Operations']) * scale
                    fields['Revenue'] = {'method': 'finance_sector_revenue_fallback', 'header': 'Revenue from Finance Sector Operations'}
                cl, ncl = amounts.get('CurrentLiabilities'), amounts.get('NonCurrentLiabilities')
                if cl is not None and ncl is not None:
                    derived = cl + ncl
                    if amounts.get('TotalLiabilities') is None:
                        amounts['TotalLiabilities'] = derived
                        fields['TotalLiabilities'] = {'method': 'CurrentLiabilities + NonCurrentLiabilities'}
                    elif abs(amounts['TotalLiabilities'] - derived) > Decimal(scale) * 3:
                        issues.append('liability_subtotals_do_not_reconcile')
                assets, equity, liabilities = (amounts.get(f) for f in ('TotalAssets', 'Equity', 'TotalLiabilities'))
                if all(v is not None for v in (assets, equity, liabilities)) and abs(assets - equity - liabilities) > Decimal(scale) * 3:
                    issues.append('balance_sheet_does_not_balance')
                ocf, capex, da, op = (amounts.get(f) for f in ('OperatingCashFlow', 'CapexCashOutflow', 'OperatingDepreciationAmortization', 'OperatingIncome'))
                if capex is not None and capex < 0:
                    issues.append('CapexCashOutflow must be a positive cash outflow magnitude')
                if amounts.get('FreeCashFlow') is None and ocf is not None and capex is not None and capex >= 0:
                    amounts['FreeCashFlow'] = ocf - capex
                    fields['FreeCashFlow'] = {'method': 'OperatingCashFlow - CapexCashOutflow; PPE and intangible cash purchases'}
                if amounts.get('Ebitda') is None and op is not None and da is not None:
                    if da < 0:
                        issues.append('OperatingDepreciationAmortization must be nonnegative')
                    else:
                        amounts['Ebitda'] = op + da
                        fields['Ebitda'] = {'method': 'OperatingIncome + OperatingDepreciationAmortization; reviewed same-scope operating expense only'}
                for target, amount in amounts.items():
                    if target in values:
                        precision, decimals = (14, 4) if target == 'Eps' else (22, 2) if target == 'SharesOutstanding' else (22, 4)
                        values[target], issue = target_decimal(amount, precision, decimals)
                        if issue:
                            issues.append(f'{target}: {issue}')
            missing = [f for f in TARGET_FIELDS if f not in NULLABLE_FIELDS and values[f] is None]
            source = {'provider': 'kap_compare', 'parser_version': VERSION, 'workbook_sha256': sorted(group['hashes']),
                      'notification_id': notification, 'published_at': published.isoformat(), 'observed_at': captured,
                      'currency': code, 'currency_scale': scale, 'scope': row_scope, 'statement_type': statement,
                      'net_income_basis': net_income_basis,
                      'period_basis': 'YTD', 'balance_sheet_basis': 'period_end', 'period_dates_inferred': True,
                      'measuring_unit_date': end.isoformat(), 'fields': fields, 'raw_headers': row,
                      'warning': 'Latest current-column snapshots; not a point-in-time or restatement-complete history. Do not subtract IAS29 YTD periods without matching measuring units.'}
            mapped = {'table': 'CompanyFundamental', 'ticker': ticker, 'values': values, 'source': source,
                      'missing_required_fields': missing, 'validation_issues': sorted(set(issues))}
            rank = (row_scope == 'consolidated', published, int(notification))
            key = (ticker, year, quarter)
            if key not in selected or rank > selected[key][0]:
                selected[key] = (rank, mapped)
        except Exception as exc:
            errors.append({'ticker': ticker, 'notification_id': notification, 'error': str(exc)})
    records = [selected[k][1] for k in sorted(selected)]
    return {'schema_version': 1, 'kind': 'warehouse_bundle', 'parser_version': VERSION, 'http_attempts': 0,
            'source_workbooks': len(seen), 'records': records, 'errors': errors,
            'summary': {'records': len(records), 'ready': sum(not r['missing_required_fields'] and not r['validation_issues'] for r in records),
                        'missing_fields': dict(Counter(f for r in records for f in r['missing_required_fields']))}}
