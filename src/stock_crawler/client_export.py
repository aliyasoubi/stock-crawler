"""Offline mapping of saved KAP comparison workbooks to a client's field contract.

This is a review export, not a database loader. The observed ten-item workbook
cannot satisfy all of CompanyFundamental's NOT NULL columns.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, ROUND_HALF_UP, localcontext
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .config import Settings, SettingsError, parse_ticker_argument
from .kap import SourceError, UnknownTicker
from .kap_export import CompanyRegistry, parse_export_row, read_export, row_candidate
from .metrics import identity_checks
from .parser import PARSER_VERSION
from .storage import dump_json, sha256_bytes, write_atomic

TARGET_FIELDS = (
    'CompanyId', 'FiscalYear', 'FiscalQuarter', 'PeriodEndDate', 'PublishDate',
    'Revenue', 'OperatingIncome', 'NetIncome', 'Ebitda', 'TotalAssets',
    'TotalLiabilities', 'Equity', 'TotalDebtShort', 'TotalDebtLong',
    'CashAndEquivalents', 'CurrentLiabilities', 'NonCurrentLiabilities',
    'FreeCashFlow', 'Eps', 'SharesOutstanding',
)
NULLABLE_FIELDS = {'TotalLiabilities', 'CurrentLiabilities', 'NonCurrentLiabilities'}
UNAVAILABLE_FIELDS = (
    'OperatingIncome', 'Ebitda', 'TotalDebtShort', 'TotalDebtLong',
    'CashAndEquivalents', 'FreeCashFlow', 'Eps', 'SharesOutstanding',
)
DIRECT_FIELDS = {
    'Revenue': 'revenue', 'TotalAssets': 'total_assets', 'Equity': 'total_equity',
    'CurrentLiabilities': 'current_liabilities',
    'NonCurrentLiabilities': 'non_current_liabilities',
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SettingsError(f'duplicate CompanyId mapping key: {key}')
        result[key] = value
    return result


def load_company_map(path: Path | None) -> dict[str, int]:
    """IDs belong to the client's dbo.Company, never inferred from ticker order."""
    if path is None:
        return {}
    result = json.loads(path.read_text('utf-8-sig'), object_pairs_hook=_unique_object)
    if not isinstance(result, dict):
        raise SettingsError('company map must be a JSON object: ticker -> client CompanyId')
    seen = set()
    for ticker, company_id in result.items():
        if not re.fullmatch(r'[A-Z0-9]{2,10}', ticker):
            raise SettingsError('company map keys must be normalized ticker symbols')
        if type(company_id) is not int or not 1 <= company_id <= 2147483647:
            raise SettingsError(f'{ticker}: CompanyId must be a positive SQL INT')
        if company_id in seen:
            raise SettingsError('multiple tickers map to the same CompanyId; review identities first')
        seen.add(company_id)
    return result


def target_decimal(value: Decimal | None, precision=22, scale=4):
    """Return an exact JSON decimal string and any lossy/range diagnostic."""
    if value is None:
        return None, None
    if not value.is_finite():
        return None, 'non_finite'
    with localcontext() as ctx:
        ctx.prec = 80
        limit = Decimal(10) ** (precision - scale)
        if abs(value) >= limit:
            return None, 'out_of_range'
        rounded = value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
        if abs(rounded) >= limit:
            return None, 'out_of_range'
        return format(rounded, f'.{scale}f'), ('rounding_required' if rounded != value else None)


def map_record(row, identity, parsed, *, workbook_hash, company_map, currency, net_income_basis):
    record = parsed.current_period()
    candidate = row_candidate(row)
    values = {name: None for name in TARGET_FIELDS}
    status = {name: 'unavailable' for name in TARGET_FIELDS}
    issues = []
    values.update(
        CompanyId=company_map.get(identity.ticker),
        FiscalYear=record.fiscal_year,
        FiscalQuarter=4, PeriodEndDate=record.period_end_date.isoformat(),
        PublishDate=candidate.published_at.astimezone(ZoneInfo('Europe/Istanbul')).date().isoformat(),
    )
    status.update(CompanyId='client_mapping' if values['CompanyId'] is not None else 'unavailable',
                  FiscalYear='reported', FiscalQuarter='annual_period_code',
                  PeriodEndDate='inferred_calendar_year_end', PublishDate='reported_istanbul_date')
    mapping = dict(DIRECT_FIELDS)
    mapping['NetIncome'] = ('net_profit' if net_income_basis == 'total-profit'
                            else 'profit_attributable_to_owners_of_parent')
    source_values = {}
    for target, source in mapping.items():
        amount = getattr(record, source)
        source_values[target] = None if amount is None else str(amount)
        values[target], issue = target_decimal(amount)
        status[target] = 'reported' if amount is not None else 'unavailable'
        if issue:
            status[target] = issue
            issues.append(f'{target}: {issue} for DECIMAL(22,4)')
    # Revenue has two source lines. HOLDING-format issuers report industrial turnover under
    # "Revenue" and finance-sector turnover separately; GENERAL-format investment/brokerage
    # holdings report only the finance line. Which line filled the client's single column is
    # stated in field_status so the client can accept or override the choice.
    if record.finance_sector_revenue is not None:
        source_values['FinanceSectorRevenue'] = str(record.finance_sector_revenue)
        if record.revenue is None:
            values['Revenue'], issue = target_decimal(record.finance_sector_revenue)
            status['Revenue'] = issue or 'finance_sector_revenue_reported_as_revenue'
            if issue:
                issues.append(f'Revenue: {issue} for DECIMAL(22,4)')
        elif status['Revenue'] == 'reported':
            status['Revenue'] = 'reported_excluding_finance_sector_revenue'
    if record.current_liabilities is not None and record.non_current_liabilities is not None:
        with localcontext() as ctx:
            ctx.prec = 80
            liabilities = record.current_liabilities + record.non_current_liabilities
        source_values['TotalLiabilities'] = str(liabilities)
        values['TotalLiabilities'], issue = target_decimal(liabilities)
        status['TotalLiabilities'] = issue or 'derived_current_plus_noncurrent_liabilities'
        if issue:
            issues.append(f'TotalLiabilities: {issue} for DECIMAL(22,4)')
    if record.currency_code != currency:
        issues.append(f'currency_mismatch: source {record.currency_code}, requested {currency}; no conversion performed')
    with localcontext() as ctx:
        ctx.prec = 80
        issues.extend(identity_checks(record.financial_values(), tolerance=Decimal(record.currency_scale) * 3))
    missing = [name for name in TARGET_FIELDS if name not in NULLABLE_FIELDS and values[name] is None]
    return {
        'ticker': identity.ticker,
        'values': values,
        'missing_required_fields': missing,
        'field_status': status,
        'validation_issues': issues,
        'source': {
            'workbook_sha256': workbook_hash, 'notification_id': candidate.notification_id,
            'published_at': candidate.published_at.isoformat(),
            'currency_code': record.currency_code, 'source_currency_scale': record.currency_scale,
            'amounts_are_base_currency_units': True,
            'consolidation_scope': candidate.consolidation_scope.value,
            'statement_type': candidate.statement_type,
            'period_basis': 'annual_year_to_date_not_standalone_Q4',
            'period_end_date_basis': 'inferred_from_year_calendar_year_assumption',
            'net_income_basis': net_income_basis,
            'unrounded_mapped_amounts': source_values,
            'parser_warnings': parsed.warnings,
        },
    }


def _error_category(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, UnknownTicker):
        return 'unmatched_company_title'
    if 'reviewed mapping' in message:
        return 'unsupported_statement_type'
    if 'NON_CALENDAR' in message:
        return 'non_calendar_year_issuer'
    if message.startswith('missing required values'):
        return 'missing_required_source_values'
    return 'invalid_row'


SCOPE_POLICIES = ('consolidated-else-unconsolidated', 'consolidated', 'unconsolidated')


def build_export(paths, *, settings: Settings, company_map=None, currency='TRY',
                 scope='consolidated-else-unconsolidated', net_income_basis='total-profit', tickers=None):
    """`scope` is a policy. The strict values keep only that statement scope. The default
    prefers consolidated and, for a company-year that has no consolidated statement at all,
    takes the unconsolidated one: an issuer without subsidiaries files only solo statements,
    and 250 of the 611 issuers in the September 19 workbooks are in that position. Which scope
    filled a record is written to its provenance; the client's table has no scope column."""
    if not re.fullmatch(r'[A-Z]{3}', currency):
        raise SettingsError('currency must be a three-letter uppercase code, e.g. TRY')
    if scope not in SCOPE_POLICIES:
        raise SettingsError('scope must be one of ' + ', '.join(SCOPE_POLICIES))
    preferred = 'unconsolidated' if scope == 'unconsolidated' else 'consolidated'
    fallback = 'unconsolidated' if scope == 'consolidated-else-unconsolidated' else None
    if net_income_basis not in ('total-profit', 'owners-of-parent'):
        raise SettingsError('invalid NetIncome basis')
    registry = CompanyRegistry(settings.kap_company_registry, settings.kap_alias_file)
    selected, errors, seen_hashes = {}, [], set()
    skipped_scope, source_rows = 0, 0
    # The same notification recurs in many saved workbooks (overlapping runs), so one problem
    # row would otherwise be reported once per workbook. Key: notification, scope, message.
    seen_row_errors: set[tuple] = set()
    for path in sorted(set(Path(p) for p in paths)):
        try:
            if path.stat().st_size > 20 * 1024 * 1024:
                raise SourceError('source workbook exceeds 20 MiB')
            data = path.read_bytes()
            digest = sha256_bytes(data)
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            rows = read_export(data)
        except (OSError, SourceError) as exc:
            errors.append({'file': str(path), 'error': str(exc)})
            continue
        for row in rows:
            source_rows += 1
            try:
                identity = registry.match(row['Company'])
                if tickers and identity.ticker not in tickers:
                    continue
                parsed = parse_export_row(row,
                    calendar_year_confirmed=settings.calendar_year_confirmed(identity.ticker),
                    parser_version=PARSER_VERSION)
                if parsed.parse_status.value != 'valid':
                    raise SourceError('; '.join(parsed.errors))
                row_scope = parsed.consolidation_scope.value
                if row_scope != preferred and row_scope != fallback:
                    skipped_scope += 1
                    continue
                mapped = map_record(row, identity, parsed, workbook_hash=digest,
                    company_map=company_map or {}, currency=currency, net_income_basis=net_income_basis)
                mapped['source']['scope_basis'] = ('preferred_scope' if row_scope == preferred
                                                   else 'fallback_only_scope_available')
                candidate = row_candidate(row)
                key = (identity.ticker, candidate.fiscal_year, 4)
                # Preferred scope outranks the fallback whatever the publication order.
                rank = (row_scope == preferred, candidate.published_at, int(candidate.notification_id))
                previous = selected.get(key)
                if previous is None or rank > previous[0]:
                    selected[key] = (rank, mapped)
                elif rank == previous[0]:
                    # Different bytes can contain the same statement. Preserve any
                    # conflicting amounts at an equal publication/notification rank.
                    if (mapped['values'] != previous[1]['values'] or
                        mapped['source']['unrounded_mapped_amounts'] != previous[1]['source']['unrounded_mapped_amounts'] or
                        mapped['source']['currency_code'] != previous[1]['source']['currency_code']):
                        previous[1]['validation_issues'].append('conflicting_same_version: select one verified source workbook')
            except (SourceError, ValueError, KeyError) as exc:
                key = (row.get('Notification ID'), row.get('Nature of Financial Statement'), str(exc))
                if key in seen_row_errors:
                    continue
                seen_row_errors.add(key)
                errors.append({
                    'type': 'row_skipped',
                    'category': _error_category(exc),
                    'company': row.get('Company'),
                    'notification_id': row.get('Notification ID'),
                    'fiscal_year': row.get('Year'),
                    'error': str(exc)
                })
                continue
    records = [entry[1] for key, entry in sorted(selected.items())]
    fallback_records = sum(r['source']['scope_basis'] == 'fallback_only_scope_available' for r in records)
    found = {r['ticker'] for r in records}
    for ticker in sorted(set(tickers or []) - found):
        errors.append({'ticker': ticker, 'error': 'no valid row in selected scope/source files'})
    counts = Counter(name for row in records for name in row['missing_required_fields'])
    complete = sum(not row['missing_required_fields'] and not row['validation_issues'] for row in records)
    error_categories = Counter(e.get('category', e.get('type', 'file')) for e in errors)
    return {
        'schema_version': 1, 'target_table': 'dbo.CompanyFundamental',
        'export_kind': 'review_only_no_database_writes', 'http_attempts': 0,
        'source_product': 'kap_compare', 'requested_currency': currency, 'selected_scope': scope,
        'net_income_basis': net_income_basis, 'source_workbooks': len(seen_hashes),
        'source_rows': source_rows, 'skipped_other_scope': skipped_scope,
        'scope_policy': scope, 'preferred_scope': preferred, 'fallback_scope': fallback,
        'records_from_fallback_scope': fallback_records,
        'selected_records': len(records), 'records_passing_required_fields_and_numeric_checks': complete,
        'missing_required_field_counts': dict(counts),
        'error_counts_by_category': dict(error_categories), 'errors': errors,
        'client_contract_notes': [
            'CompanyId is only supplied by an explicit client mapping; foreign-key existence is not verified here.',
            'FiscalQuarter=4 is the source annual period code. Income values cover twelve months, not Oct-Dec only.',
            'PeriodEndDate is inferred; the comparison export does not report exact period dates.',
            'Confirm NetIncome basis, EPS basis, debt/lease policy, FCF definition and shares basis with the client.',
            'Target schema omits currency, scope and source version; preserve this review report as provenance.',
            'source.scope_basis says whether a record is the preferred scope or the only scope the issuer files.',
            'Archive selection does not consult SQL withdrawal state or establish point-in-time completeness.',
        ],
        'records': records,
    }


def cmd_export_company_fundamentals(args):
    settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
    paths = args.input or sorted((settings.data_dir / 'raw' / '_exports').glob('*/source.xlsx'))
    if not paths:
        raise SettingsError('no saved workbooks found; run sync/import first or provide --input')
    protected = [settings.kap_company_registry, *paths]
    if args.company_map:
        protected.append(args.company_map)
    if args.output.resolve() in {p.resolve() for p in protected}:
        raise SettingsError('output must not overwrite a source workbook, registry or CompanyId mapping')
    report = build_export(paths, settings=settings, company_map=load_company_map(args.company_map),
        currency=args.currency, scope=args.scope, net_income_basis=args.net_income_basis,
        tickers=parse_ticker_argument(args.tickers) if args.tickers else None)
    write_atomic(args.output, dump_json(report).encode('utf-8'))
    print(f"Client review: {report['selected_records']} records; "
          f"{report['records_passing_required_fields_and_numeric_checks']} pass required-field/numeric checks; "
          f"{len(report['errors'])} distinct skipped rows; 0 HTTP attempts")
    print(f"  scope policy {report['scope_policy']}: {report['records_from_fallback_scope']} records use the fallback scope; "
          f"{report['skipped_other_scope']} rows of other scopes skipped")
    for category, count in sorted(report['error_counts_by_category'].items()):
        print(f'  skipped {category}: {count}')
    for field, count in report['missing_required_field_counts'].items():
        print(f'  missing {field}: {count} records')
    for record in report['records']:
        for issue in record['validation_issues']:
            print(f"  {record['ticker']} {record['values']['FiscalYear']}: {issue}")
    print(f'Review JSON: {args.output}')
    print('Review export only: no client table was created or written.')
    fatal_errors = [
        e for e in report['errors']
        if e.get('type') != 'row_skipped'
    ]

    return 0 if (
        report['selected_records'] > 0
        and not fatal_errors
        and report['records_passing_required_fields_and_numeric_checks']
            == report['selected_records']
    ) else 1
