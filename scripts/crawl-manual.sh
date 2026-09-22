#!/usr/bin/env bash
# Manual annual comparison sync; reads local source using the existing Docker image.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

usage() {
  cat <<'HELP'
Usage: bash scripts/crawl-manual.sh [options]
  --tickers LIST       Comma-separated tickers; overrides the watchlist.
  --years LIST         1-5 annual years, comma-separated (default: 2025).
  --limit NUMBER       Maximum due companies, 1-100 (default: 25).
  --refresh            Ignore company freshness once; not for routine runs.
  --dry-run            Print the crawler command without starting Docker/network.
  --help              Show this help.

Default watchlist: config/companies_manual.txt
The command saves comparison snapshots, parses and persists supported fields
in the existing crawler database. It does NOT fetch full PDFs or write the
client's CompanyFundamental table. Missing source fields stay missing.
HELP
}

tickers=''
years='2025'
limit='25'
refresh=0
dry_run=0
while (( $# )); do
  case "$1" in
    --tickers|--years|--limit)
      (( $# >= 2 )) || { echo "Missing value for $1" >&2; exit 2; }
      [[ -n "$2" && "$2" != --* ]] || { echo "Missing value for $1" >&2; exit 2; }
      case "$1" in
        --tickers) tickers="$2" ;;
        --years) years="$2" ;;
        --limit) limit="$2" ;;
      esac
      shift 2 ;;
    --refresh) refresh=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
command -v python3 >/dev/null || { echo 'Python 3 is required on the Ubuntu host.' >&2; exit 2; }
[[ "$limit" =~ ^[1-9][0-9]{0,2}$ ]] && (( limit <= 100 )) || {
  echo '--limit must be an integer from 1 to 100.' >&2; exit 2;
}
years_json=$(python3 - "$years" <<'PY'
import json, sys
try:
    parts = sys.argv[1].split(',')
    if not all(len(x.strip()) == 4 and x.strip().isdigit() for x in parts):
        raise ValueError
    years = [int(x) for x in parts]
    if not (1 <= len(years) <= 5 and len(set(years)) == len(years)
            and all(2000 <= y <= 2100 for y in years)):
        raise ValueError
except ValueError:
    sys.exit('--years must contain 1-5 distinct years from 2000 to 2100, e.g. 2024,2025.')
print(json.dumps(years, separators=(',', ':')))
PY
)
selection=()
if [[ -n "$tickers" ]]; then
  tickers=$(python3 - "$tickers" <<'PY'
import re, sys
parts = [p.strip().upper() for p in sys.argv[1].split(',')]
if not parts or not all(re.fullmatch(r'[A-Z0-9]{2,10}', p) for p in parts):
    sys.exit('Invalid ticker list; use comma-separated symbols such as ASELS,THYAO.')
print(','.join(dict.fromkeys(parts)))
PY
)
  selection=(--tickers "$tickers")
else
  [[ -f config/companies_manual.txt ]] || { echo 'Missing config/companies_manual.txt.' >&2; exit 2; }
  selection=(--company-file /app/config/companies_manual.txt)
fi
for file in docker-compose.yml compose.db-local.yml compose.proxy-host.yml src/stock_crawler/crawl/cli.py; do
  [[ -f "$file" ]] || { echo "Missing project file: $file. Copy this script into your project's scripts folder." >&2; exit 2; }
done
export CRAWLER_PROXY_URL="${CRAWLER_PROXY_URL:-socks5h://127.0.0.1:10808}"
export CRAWLER_DB_HOST_PORT="${CRAWLER_DB_HOST_PORT:-1433}"
compose=(docker compose -f docker-compose.yml -f compose.db-local.yml -f compose.proxy-host.yml)
args=("${compose[@]}" run --rm --no-deps --pull never
  --volume "$PWD/src:/app/src:ro"
  --env PYTHONPATH=/app/src --env DATA_DIR=/app/data
  --env KAP_COMPANY_REGISTRY=/app/config/kap_companies.json
  --env SOURCE_MODE=kap-export --env "KAP_YEARS=$years_json"
  --env "MAX_COMPANIES_PER_RUN=$limit" --env MAX_REQUESTS_PER_RUN=10
  --env REQUEST_DELAY_MIN_SECONDS=15 --env REQUEST_DELAY_MAX_SECONDS=30
  --env REQUEST_TIMEOUT_SECONDS=60 --env MAX_RETRIES=2
  --env DISCOVERY_INTERVAL_HOURS=24
  --entrypoint python crawler -m stock_crawler.crawl.cli sync
  "${selection[@]}" --limit "$limit")
(( refresh == 0 )) || args+=(--refresh)
if (( dry_run )); then
  printf '%q ' "${args[@]}"
  printf '\n'
  exit 0
fi
[[ -f .env ]] || { echo 'Missing existing project .env; this helper is not a first-time installer.' >&2; exit 2; }
command -v flock >/dev/null || { echo 'Install util-linux to provide flock.' >&2; exit 2; }
exec 9> .crawler-manual.lock
flock -n 9 || { echo 'Another crawl-manual.sh run is active. Wait for it to finish.' >&2; exit 2; }
docker image inspect stock-crawler:local >/dev/null 2>&1 || {
  echo 'Local stock-crawler:local image is missing or Docker is inaccessible. This helper does not rebuild or pull it.' >&2; exit 2;
}
python3 - <<'PY'
import os, socket, sys
from urllib.parse import urlsplit
try:
    proxy = urlsplit(os.environ['CRAWLER_PROXY_URL'])
    if not proxy.hostname or not proxy.port:
        raise ValueError('A host and explicit port are required.')
    with socket.create_connection((proxy.hostname, proxy.port), timeout=3):
        pass
except (OSError, ValueError):
    sys.exit('Cannot connect to CRAWLER_PROXY_URL. Restart your SSH proxy, then retry. This check does not test KAP access.')
PY
"${compose[@]}" up -d --wait --pull never mssql
echo "Annual years: $years | company cap: $limit | request-attempt cap: 10"
echo 'Downloading comparison data, parsing and saving valid records to the crawler database.'
if "${args[@]}"; then
  echo 'Run completed. Check its company statuses and summary for deferred/missing records, then refresh Grafana.'
else
  rc=$?
  echo "Crawler exited with status $rc. Review its error/summary; do not repeatedly retry a blocked or throttled source." >&2
  exit "$rc"
fi
