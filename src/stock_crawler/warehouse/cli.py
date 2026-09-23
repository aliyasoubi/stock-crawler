"""Run `python -m stock_crawler.warehouse.cli --help` for the client warehouse workflow."""
from __future__ import annotations

import argparse
import base64
from datetime import date
import json
from pathlib import Path
import sys

from ..core.storage import dump_json, sha256_bytes, utcnow, write_atomic
from .loader import BIST_MARKET, COLUMNS, INDEX_NAMES, bundle, import_csv, load_bundle
from .fundamentals import build_fundamentals, load_mapping
from .sources import fetch_snapshot, evds_url, import_evds, normalize_vendor_csv


def parser():
    p = argparse.ArgumentParser(prog='stock-warehouse', description='Review-first client warehouse ingestion; FactorStore excluded')
    s = p.add_subparsers(dest='command', required=True)
    f = s.add_parser('fundamentals', help='merge complementary KAP XLSX/manifest files by notification; Q1-Q4 YTD')
    f.add_argument('--input', type=Path, nargs='+', required=True)
    f.add_argument('--registry', type=Path, default=Path('config/kap_companies.json'))
    f.add_argument('--company-map', type=Path)
    f.add_argument('--aliases', type=Path, default=Path('config/kap_aliases.json'))
    f.add_argument('--currency', default='TRY')
    f.add_argument('--calendar-tickers', default='', help='comma-separated explicitly verified calendar-year issuers')
    f.add_argument('--mapping', type=Path, help='reviewed additional concept mapping JSON')
    f.add_argument('--net-income-basis', choices=['total-profit', 'owners-of-parent'], default='total-profit')
    f.add_argument('--scope', choices=['consolidated-else-unconsolidated', 'consolidated', 'unconsolidated'], default='consolidated-else-unconsolidated')
    f.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    f.add_argument('--output', type=Path, required=True)
    c = s.add_parser('csv', help='import normalized target-column CSV')
    c.add_argument('--input', type=Path, required=True)
    c.add_argument('--table', choices=[t for t in COLUMNS if t != 'CompanyFundamental'], required=True)
    c.add_argument('--source', type=Path, required=True, help='provider and semantic metadata JSON')
    c.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    c.add_argument('--output', type=Path, required=True)
    v = s.add_parser('vendor-csv', help='map an explicitly reviewed BIST/vendor CSV or JSON profile')
    v.add_argument('--input', type=Path, required=True)
    v.add_argument('--profile', type=Path, required=True)
    v.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    v.add_argument('--output', type=Path, required=True)
    e = s.add_parser('evds', help='parse EVDS saved JSON or fetch one native-frequency request')
    e.add_argument('--input', type=Path, required=True, help='source JSON path; --fetch writes here')
    e.add_argument('--profile', type=Path, required=True)
    e.add_argument('--market-id', type=int, required=True)
    e.add_argument('--fetch', action='store_true')
    e.add_argument('--start', type=date.fromisoformat)
    e.add_argument('--end', type=date.fromisoformat)
    e.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    e.add_argument('--output', type=Path, required=True)
    d = s.add_parser('fetch', help='download exactly one approved source URL, bounded; retain source metadata')
    d.add_argument('--url', required=True)
    d.add_argument('--output', type=Path, required=True)
    d.add_argument('--key-env', help='environment variable name for TCMB header key')
    fx = s.add_parser('tcmb-fx', help='fetch/replay official daily indicative USD buying rate; no API key')
    fx.add_argument('--input', type=Path, required=True, help='XML file; --fetch writes here')
    fx.add_argument('--fetch', action='store_true')
    fx.add_argument('--date', type=date.fromisoformat, help='optional exact source day; omit for latest published day')
    fx.add_argument('--market-id', type=int, required=True)
    fx.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    fx.add_argument('--output', type=Path, required=True)
    md = s.add_parser('isyatirim-daily', help='fetch latest public BIST equity/index snapshot; run after market close')
    md.add_argument('--company-map', type=Path, help='exported ticker -> CompanyId JSON')
    md.add_argument('--index-map', type=Path, help='exported index code -> IndexId JSON')
    company_selection = md.add_mutually_exclusive_group()
    company_selection.add_argument('--tickers', help='comma-separated subset; default is every company-map key')
    company_selection.add_argument('--company-file', type=Path, help='ticker subset file')
    md.add_argument('--indices', help='comma-separated subset; default is every index-map key')
    md.add_argument('--source-priority', type=int, default=2, help='lower wins; BIST official should remain priority 1')
    md.add_argument('--batch-size', type=int, default=20, help='symbols per request, 1..20 (provider limit)')
    md.add_argument('--pause-seconds', type=float, default=1.0, help='polite delay between batches, 0..60')
    md.add_argument('--max-symbols', type=int, help='pilot safety limit applied after selection')
    md.add_argument('--allow-intraday', action='store_true', help='allow today before 18:15 Türkiye time; not recommended')
    md.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    md.add_argument('--output', type=Path, required=True)
    mh = s.add_parser('isyatirim-history', help='backfill daily MarketData history; one request per company. This product reports no opening price and no share count: OpenPrice is NULL and Volume is derived from turnover / VWAP')
    mh.add_argument('--company-map', type=Path, required=True, help='exported ticker -> CompanyId JSON')
    history_selection = mh.add_mutually_exclusive_group()
    history_selection.add_argument('--tickers', help='comma-separated subset; default is every company-map key')
    history_selection.add_argument('--company-file', type=Path, help='ticker subset file')
    mh.add_argument('--start', required=True, help='first trade date, YYYY-MM-DD')
    mh.add_argument('--end', required=True, help='last trade date, YYYY-MM-DD')
    mh.add_argument('--source-priority', type=int, default=3, help='lower wins; weaker than the daily snapshot (2) because Volume is derived and OpenPrice is absent')
    mh.add_argument('--max-symbols', type=int, help='pilot safety limit applied after selection')
    mh.add_argument('--overwrite', action='store_true', help='refetch every company; default resumes only symbols whose stored bundle already covers this exact request')
    mh.add_argument('--env-file', type=Path, help='settings file supplying request pacing and the per-run request budget')
    mh.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    mh.add_argument('--output-dir', type=Path, required=True, help='one bundle JSON per company; load them individually')
    ls = s.add_parser('listing-status', help='survey the public quote feed and set Company.IsActive=1 for tickers the exchange actually quotes; unquoted registrants keep IsActive NULL')
    ls.add_argument('--company-map', type=Path, required=True, help='exported ticker -> CompanyId JSON')
    ls.add_argument('--registry', type=Path, default=Path('config/kap_companies.json'), help='supplies FullName for each ticker')
    ls.add_argument('--market-id', type=int, required=True)
    ls.add_argument('--batch-size', type=int, default=20, help='symbols per request, 1..20 (provider limit)')
    ls.add_argument('--pause-seconds', type=float, default=1.2, help='polite delay between batches, 0..60')
    ls.add_argument('--data-dir', type=Path, default=Path('data/warehouse'))
    ls.add_argument('--output', type=Path, required=True)
    l = s.add_parser('load', help='dry-run by default; --apply stages and promotes eligible rows to existing client tables')
    l.add_argument('--input', type=Path, required=True)
    l.add_argument('--apply', action='store_true')
    l.add_argument('--allow-partial', action='store_true', help='allow missing financial metrics only where SQL columns are nullable; never invent values')
    l.add_argument('--env-file', type=Path, help='dedicated client warehouse connection settings')
    l.add_argument('--output', type=Path)
    k = s.add_parser('kap-script', help='quarterly browser exporter; additional IDs from captured request only')
    k.add_argument('--registry', type=Path, default=Path('config/kap_companies.json'))
    selection = k.add_mutually_exclusive_group(required=True)
    selection.add_argument('--tickers')
    selection.add_argument('--company-file', type=Path)
    k.add_argument('--years', required=True)
    k.add_argument('--periods', default='1,2,3,4')
    k.add_argument('--request', type=Path, help='captured KAP request JSON; reuse its itemIdList')
    k.add_argument('--max-requests', type=int, default=5)
    k.add_argument('--batch-size', type=int, default=25)
    k.add_argument('--output', type=Path, required=True)
    r = s.add_parser('reference', help='prepare market/index seeds and registry company rows; unknown metadata stays null')
    r.add_argument('--registry', type=Path, default=Path('config/kap_companies.json'))
    r.add_argument('--company-map', type=Path)
    r.add_argument('--index-map', type=Path, help='IndexCode -> actual client IndexId')
    r.add_argument('--market-id', type=int, required=True)
    r.add_argument('--output', type=Path, required=True)
    for name, help_text in (
        ('init-db', 'create missing warehouse tables/staging in an existing DB; requires DDL rights'),
        ('seed-reference', 'idempotently seed BIST/four indices and optional registry company candidates'),
        ('inspect', 'show actual database, row counts, required columns and load status'),
        ('export-maps', 'export real warehouse IDs and a company metadata CSV for enrichment'),
    ):
        a = s.add_parser(name, help=help_text)
        a.add_argument('--env-file', type=Path,
                       help='optional external warehouse settings; default: Docker/.env settings')
        if name in ('init-db', 'seed-reference'):
            a.add_argument('--apply', action='store_true', help='execute database writes')
        if name == 'seed-reference':
            a.add_argument('--registry', type=Path, default=Path('config/kap_companies.json'))
            choices = a.add_mutually_exclusive_group()
            choices.add_argument('--tickers')
            choices.add_argument('--company-file', type=Path)
            choices.add_argument('--all-registry', action='store_true')
        if name == 'export-maps':
            a.add_argument('--output-dir', type=Path, default=Path('config/warehouse'))
    return p


def read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def expand_inputs(paths, root):
    """Replay browser batches without passing expanded headers to the annual pipeline."""
    outputs = []
    for path in paths:
        if path.suffix.lower() != '.json':
            outputs.append(path)
            continue
        if path.stat().st_size > 200 * 1024 * 1024:
            raise ValueError('manifest exceeds 200 MiB')
        document = read_json(path)
        batches = document if isinstance(document, list) else document.get('batches')
        if not isinstance(batches, list) or len(batches) > 1000:
            raise ValueError('invalid KAP manifest')
        for batch in batches:
            encoded = batch['base64']
            if len(encoded) > 28 * 1024 * 1024:
                raise ValueError('encoded XLSX exceeds size limit')
            data = base64.b64decode(encoded, validate=True)
            digest = sha256_bytes(data)
            if batch.get('sha256') and batch['sha256'] != digest:
                raise ValueError('manifest hash mismatch')
            target = root / 'raw' / 'kap_compare' / digest / 'source.xlsx'
            write_atomic(target, data)
            outputs.append(target)
    return outputs


def history_request_covered(path, ticker, company_id, start, end, version):
    """True when an existing bundle already answers exactly this request.

    Resume must not turn on the mere existence of `<ticker>.json`. A file written for a
    narrower date range, a different CompanyId map or an older parser does not satisfy a
    widened request, and a run that stopped mid-symbol left no completion marker at all.
    Anything unreadable, unmarked or narrower is refetched rather than silently skipped.
    """
    try:
        previous = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return False
    request = previous.get('request')
    if not isinstance(request, dict) or request.get('completed') is not True:
        return False
    return (request.get('symbol') == ticker and request.get('company_id') == company_id
            and request.get('parser_version') == version
            and str(request.get('start', '')) <= start.isoformat()
            and str(request.get('end', '')) >= end.isoformat())


def run_isyatirim_history(args):
    """Backfill one bundle per company, one bundle per output file.

    Resumable: a symbol is skipped only when its stored bundle already covers this exact
    request. Kept out of `run` because it writes many outputs rather than one, and because
    a stop signal (budget, cooldown, block) must end the run with the remaining work named
    instead of continuing a request storm across hundreds of symbols.
    """
    from datetime import date
    import httpx
    from ..core.config import Settings, load_company_file, parse_ticker_argument
    from ..core.storage import StateStore
    from ..crawl.client_export import load_company_map
    from ..crawl.fetch import AccessBlocked, BudgetExhausted, FetchError, HostCoolingDown, HostThrottled, PacedClient
    from .prices import HISTORY_HOSTS, HISTORY_VERSION, build_isyatirim_history

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end < start:
        raise ValueError('--end precedes --start')
    company_map = load_company_map(args.company_map)
    tickers = (parse_ticker_argument(args.tickers) if args.tickers else
               load_company_file(args.company_file) if args.company_file else
               sorted(company_map))
    unknown = sorted(set(tickers) - set(company_map))
    if unknown:
        raise ValueError(f'tickers missing from the exported CompanyId map: {unknown}')
    if args.max_symbols is not None:
        if args.max_symbols < 1:
            raise ValueError('--max-symbols must be positive')
        tickers = tickers[:args.max_symbols]

    settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
    client = httpx.Client(headers={'User-Agent': settings.http_user_agent})
    fetcher = PacedClient(client, settings, StateStore(args.data_dir), allowed_hosts=HISTORY_HOSTS)
    written, skipped, failed, pending = [], [], [], []
    stopped = None
    try:
        for ticker in tickers:
            target = args.output_dir / f'{ticker}.json'
            if (not args.overwrite and target.exists() and history_request_covered(
                    target, ticker, company_map[ticker], start, end, HISTORY_VERSION)):
                skipped.append(ticker)
                continue
            if stopped is not None:
                pending.append(ticker)
                continue
            try:
                result = build_isyatirim_history(ticker, company_map[ticker], start=start, end=end,
                    fetcher=fetcher, archive_dir=args.data_dir, source_priority=args.source_priority)
            except (BudgetExhausted, HostThrottled, AccessBlocked, HostCoolingDown) as exc:
                stopped = str(exc)
                pending.append(ticker)
                continue
            except (FetchError, ValueError, TypeError) as exc:
                failed.append({'ticker': ticker, 'error': str(exc)})
                continue
            summary = result.get('summary', {})
            document = dict(result, request={'symbol': ticker, 'company_id': company_map[ticker],
                'start': start.isoformat(), 'end': end.isoformat(),
                'parser_version': HISTORY_VERSION, 'source_priority': args.source_priority,
                'completed': summary.get('ready', 0) > 0})
            write_atomic(target, dump_json(document).encode())
            written.append({'ticker': ticker, 'rows': summary.get('records', 0),
                            'ready': summary.get('ready', 0), 'errors': len(result.get('errors', []))})
            print(f"  {ticker:<8} {summary.get('records', 0):>5} rows  {summary.get('ready', 0):>5} ready  "
                  f"{len(result.get('errors', [])):>3} skipped  -> {target}")
    finally:
        fetcher.close()

    # A bundle with no loadable row is a failed symbol, not a success: its output file is
    # marked incomplete so the next run refetches it instead of resuming past it forever.
    empty = [item['ticker'] for item in written if not item['ready']]
    print(dump_json({'companies_written': len(written), 'companies_skipped_covered': len(skipped),
                     'companies_failed': len(failed), 'companies_pending': len(pending),
                     'companies_empty': empty,
                     'rows_written': sum(item['rows'] for item in written),
                     'rows_ready': sum(item['ready'] for item in written),
                     'rows_skipped': sum(item['errors'] for item in written),
                     'http_attempts': fetcher.attempts, 'stopped_reason': stopped,
                     'open_price': 'NULL for every backfilled row; this product reports no opening price',
                     'output_dir': str(args.output_dir)}))
    for item in failed:
        print(f"  failed {item['ticker']}: {item['error']}", file=sys.stderr)
    for ticker in empty:
        print(f'empty {ticker}: no loadable row in the requested range', file=sys.stderr)
    if stopped:
        print(f'stopped: {stopped}; rerun the same command to resume ({len(pending)} companies pending)', file=sys.stderr)
        return 2
    # Nothing to do is success: a fully covered rerun is the normal steady state.
    return 1 if failed or empty else 0


def run(args):
    from ..crawl.client_export import load_company_map
    if args.command in ('init-db', 'seed-reference', 'inspect', 'export-maps'):
        from .admin import warehouse_settings, initialize, seed_reference, inspect_database, export_maps
        from ..core.db import make_engine
        settings = warehouse_settings(args.env_file, use_bootstrap=args.command == 'init-db')
        companies = []
        if args.command == 'seed-reference' and (args.tickers or args.company_file or args.all_registry):
            from ..crawl.kap_export import CompanyRegistry
            from ..core.config import load_company_file, parse_ticker_argument
            registry = CompanyRegistry(args.registry)
            selected = (parse_ticker_argument(args.tickers) if args.tickers else
                        load_company_file(args.company_file) if args.company_file else
                        [e['ticker'] for e in registry.entries])
            for ticker in selected:
                identity = registry.resolve(ticker)
                companies.append({'ticker': identity.ticker, 'company_name': identity.company_name})
        if args.command in ('init-db', 'seed-reference') and not args.apply:
            print(dump_json({'mode': 'dry_run_no_sql', 'command': args.command,
                             'database': settings.mssql_database, 'company_candidates': len(companies),
                             'note': 'Add --apply to execute. No database connection opened.'}))
            return 0
        engine = make_engine(settings)
        try:
            if args.command == 'init-db':
                result = initialize(engine)
            elif args.command == 'seed-reference':
                result = seed_reference(engine, companies)
            elif args.command == 'inspect':
                result = inspect_database(engine)
            else:
                result = export_maps(engine, args.output_dir)
        finally:
            engine.dispose()
        print(dump_json(result))
        return 0
    if getattr(args, 'output', None):
        inputs = []
        for name in ('input', 'registry', 'company_map', 'index_map', 'profile', 'source', 'mapping', 'request', 'env_file'):
            value = getattr(args, name, None)
            inputs.extend(value if isinstance(value, list) else [value] if value is not None else [])
        if args.output.resolve() in {p.resolve() for p in inputs}:
            raise ValueError('output must not overwrite a source or configuration file')
    if args.command == 'fundamentals':
        paths = expand_inputs(args.input, args.data_dir)
        result = build_fundamentals(paths, registry_path=args.registry,
            company_map=load_company_map(args.company_map), currency=args.currency,
            calendar_tickers=args.calendar_tickers.split(','), mapping=load_mapping(args.mapping),
            net_income_basis=args.net_income_basis, scope=args.scope, archive_dir=args.data_dir, aliases_path=args.aliases)
    elif args.command == 'csv':
        result = import_csv(args.input, args.table, source=read_json(args.source), archive_dir=args.data_dir)
    elif args.command == 'vendor-csv':
        result = normalize_vendor_csv(args.input, read_json(args.profile), archive_dir=args.data_dir)
    elif args.command == 'isyatirim-daily':
        from ..core.config import load_company_file, parse_ticker_argument
        from .prices import build_isyatirim_daily
        company_map = load_company_map(args.company_map) if args.company_map else {}
        index_map = read_json(args.index_map) if args.index_map else {}
        if not isinstance(index_map, dict):
            raise ValueError('--index-map must contain a JSON object')
        companies = (parse_ticker_argument(args.tickers) if args.tickers else
                     load_company_file(args.company_file) if args.company_file else
                     list(company_map))
        indices = parse_ticker_argument(args.indices) if args.indices else list(index_map)
        if args.max_symbols is not None:
            if args.max_symbols < 1:
                raise ValueError('--max-symbols must be positive')
            selected = [("company", c) for c in companies] + [("index", c) for c in indices]
            selected = selected[:args.max_symbols]
            companies = [c for kind, c in selected if kind == 'company']
            indices = [c for kind, c in selected if kind == 'index']
        result = build_isyatirim_daily(company_map=company_map, index_map=index_map,
            company_codes=companies, index_codes=indices, archive_dir=args.data_dir,
            source_priority=args.source_priority, batch_size=args.batch_size,
            pause_seconds=args.pause_seconds, allow_intraday=args.allow_intraday)
    elif args.command == 'isyatirim-history':
        return run_isyatirim_history(args)
    elif args.command == 'listing-status':
        from ..crawl.kap_export import CompanyRegistry
        from .prices import build_listing_status
        registry = CompanyRegistry(args.registry)
        company_map = load_company_map(args.company_map)
        unknown = sorted(set(company_map) - set(registry.by_ticker))
        if unknown:
            raise ValueError(f'CompanyId map holds tickers absent from the registry: {unknown}')
        result = build_listing_status(company_map=company_map,
            names={t: registry.by_ticker[t]['company_name'] for t in company_map},
            market_id=args.market_id, archive_dir=args.data_dir,
            batch_size=args.batch_size, pause_seconds=args.pause_seconds)
        survey = result['listing_survey']
        print(f"surveyed {survey['companies_surveyed']}: {survey['quoted']} quoted, "
              f"{survey['unquoted']} unquoted (IsActive left NULL)")
    elif args.command == 'evds':
        profile = read_json(args.profile)
        observed = None
        if args.fetch:
            if not args.start or not args.end:
                raise ValueError('--fetch requires --start and --end')
            meta = fetch_snapshot(evds_url(profile, args.start, args.end), args.input, key_env='EVDS_API_KEY')
            observed = meta['observed_at']
        result = import_evds(args.input, profile, market_id=args.market_id, archive_dir=args.data_dir, observed_at=observed)
    elif args.command == 'fetch':
        result = fetch_snapshot(args.url, args.output, key_env=args.key_env)
        print(dump_json(result))
        return 0
    elif args.command == 'tcmb-fx':
        from .sources import tcmb_fx_url, import_tcmb_fx
        observed = None
        if args.fetch:
            observed = fetch_snapshot(tcmb_fx_url(args.date), args.input)['observed_at']
        result = import_tcmb_fx(args.input, market_id=args.market_id, archive_dir=args.data_dir,
                                expected_date=args.date, observed_at=observed)
    elif args.command == 'load':
        engine = None
        if args.apply:
            from .admin import warehouse_settings
            from ..core.db import make_engine
            engine = make_engine(warehouse_settings(args.env_file))
        try:
            result = load_bundle(read_json(args.input), engine=engine, apply=args.apply, allow_partial=args.allow_partial)
        finally:
            if engine is not None:
                engine.dispose()
        if args.output:
            write_atomic(args.output, dump_json(result).encode())
        print(dump_json(result))
        return 0 if not result.get('incomplete') and not result.get('source_errors') and result.get('ready', result.get('records', 0)) == result.get('records', 0) else 1
    elif args.command == 'kap-script':
        from ..crawl.browser_export import build_browser_script
        from ..crawl.kap_export import CompanyRegistry, FIELDS
        registry = CompanyRegistry(args.registry)
        items = list(v[1] for v in FIELDS.values())
        if args.request:
            items = list(dict.fromkeys(items + read_json(args.request)['itemIdList']))
        from ..core.config import load_company_file
        tickers = args.tickers.split(',') if args.tickers else load_company_file(args.company_file)
        script = build_browser_script([registry.resolve(t.strip()) for t in tickers],
            [int(y) for y in args.years.split(',')], periods=[int(q) for q in args.periods.split(',')],
            items=items, max_requests=args.max_requests, batch_size=args.batch_size)
        write_atomic(args.output, script.encode())
        print(f'Browser exporter: {args.output}; max {args.max_requests} requests per invocation')
        return 0
    elif args.command == 'reference':
        from ..crawl.kap_export import CompanyRegistry
        company_ids = load_company_map(args.company_map)
        registry = CompanyRegistry(args.registry)
        source = {'provider': 'reference_seed', 'observed_at': utcnow().isoformat(),
                  'registry_sha256': sha256_bytes(args.registry.read_bytes()),
                  'warning': 'Registry membership is not evidence of active listed equity status. Enrich metadata before loading.'}
        records = [{'table': 'Market', 'values': dict(BIST_MARKET, MarketId=args.market_id), 'source': source}]
        for entry in registry.entries:
            values = dict.fromkeys(COLUMNS['Company'])
            values.update(CompanyId=company_ids.get(entry['ticker']), Ticker=entry['ticker'],
                          MarketId=args.market_id, FullName=entry['company_name'])
            records.append({'table': 'Company', 'values': values, 'source': source,
                            'validation_issues': ['listing_status_sector_currency_and_IPO_date_require_reference_enrichment']})
        indices = read_json(args.index_map) if args.index_map else {}
        for code, name in INDEX_NAMES.items():
            records.append({'table': 'MarketIndexMaster', 'values': {'IndexId': indices.get(code),
                'IndexCode': code, 'MarketId': args.market_id, 'IndexName': name}, 'source': source})
        result = bundle(records)
    else:
        raise ValueError('unknown command')
    write_atomic(args.output, dump_json(result).encode())
    summary = result.get('summary', {})
    print(dump_json(dict(summary, errors=len(result.get('errors', [])), output=str(args.output))))
    return 0 if not result.get('errors') and summary.get('ready') == summary.get('records') and summary.get('records', 0) > 0 else 1


def main(argv=None):
    try:
        return run(parser().parse_args(argv))
    except Exception as exc:
        from sqlalchemy.exc import SQLAlchemyError
        import httpx
        # Connection exceptions may embed credentials in URLs/ODBC diagnostics.
        message = type(exc).__name__ if isinstance(exc, (SQLAlchemyError, httpx.HTTPError)) else str(exc)
        print(f'warehouse error: {message}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
