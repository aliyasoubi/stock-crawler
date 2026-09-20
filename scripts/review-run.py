#!/usr/bin/env python3
"""Inspect the newest persisted run using only the Python standard library."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summary', nargs='?', type=Path, help='exact summary.json to inspect')
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--write-pending', type=Path, metavar='FILE',
                        help='write the run\'s pending tickers (one per line) to FILE, e.g. config/companies_pending.txt')
    args = parser.parse_args()
    paths = [args.summary] if args.summary else list((args.data_dir / 'runs').glob('*/summary.json'))
    if not paths:
        print('No saved run summaries found.', file=sys.stderr)
        return 1
    path = max(paths, key=lambda p: (p.stat().st_mtime_ns, str(p)))
    data = json.loads(path.read_text('utf-8'))
    print('summary:', path)
    print('started:', data.get('started_at'), '| finished:', data.get('finished_at'))
    print('attempts:', data.get('request_attempts'), '| stopped:', data.get('stopped_reason') or 'none')
    print('cap:', data.get('effective_company_limit', 'not recorded'),
          '| selected companies:', data.get('selected_company_count', 'not recorded'))
    print('statuses (result entries, not unique companies):', dict(Counter(r.get('status') for r in data['companies'])))
    print('rejected rows:', data.get('rejected_row_count', 0),
          '| titles matched by normalization:', data.get('normalized_title_match_count', 0))
    print('excluded tickers:', data.get('excluded_ticker_count', 0))
    for row in data.get('rejected_rows', [])[:10]:
        print(' ', row['company'], '->', row['reason'])
    if data.get('rejected_row_count'):
        print('  -> `stock-crawler verify-aliases` proves historical titles with single-company requests')
    pending = list(data.get('pending') or [])
    if pending:
        print(f'pending: {len(pending)} tickers not reached before the stop; re-running the same command continues with them '
              '(never-checked and oldest-checked companies are selected first)')
    if args.write_pending:
        args.write_pending.write_text(''.join(f'{t}\n' for t in pending), encoding='utf-8')
        print('pending list written:', args.write_pending, f'({len(pending)} tickers)')
    print('This is the saved run above; compare its timestamp with the command you just ran.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
