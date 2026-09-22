#!/usr/bin/env bash
# Run from the original checkout to keep the same Compose project and SQL volume.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

mode="${1:-help}"
if (( $# )); then shift; fi
if [[ "$mode" == help || "$mode" == --help ]]; then
  cat <<'HELP'
Usage: bash scripts/crawler-ubuntu.sh MODE [crawler arguments]
  build                      Rebuild the crawler image after source changes.
  db                         Start the existing SQL container with a loopback port.
  docker probe-source        Run the crawler using Ubuntu host networking.
  docker sync --tickers THYAO,ASELS
  warehouse --help            Run the warehouse CLI using the same SQL/proxy setup.
  native probe-source        Run .venv-native/bin/stock-crawler on Ubuntu.
  native sync --tickers THYAO,ASELS
Optional exported settings:
  CRAWLER_PROXY_URL           Default socks5h://127.0.0.1:10808
  CRAWLER_DB_HOST_PORT        Default 1433; set once for both db and crawler.
HELP
  exit 0
fi
[[ -f docker-compose.yml && -f .env ]] || {
  echo 'Place these add-on files in the existing project beside docker-compose.yml and .env.' >&2
  exit 2
}

# Explicit values avoid an inherited HTTPS_PROXY or lowercase proxy taking precedence.
export CRAWLER_PROXY_URL="${CRAWLER_PROXY_URL:-socks5h://127.0.0.1:10808}"
export CRAWLER_DB_HOST_PORT="${CRAWLER_DB_HOST_PORT:-1433}"
compose=(docker compose -f docker-compose.yml -f compose.db-local.yml)
case "$mode" in
  build)
    exec "${compose[@]}" -f compose.proxy-host.yml build "$@" crawler
    ;;
  db)
    exec "${compose[@]}" up -d --wait mssql
    ;;
  docker)
    if (( $# == 0 )); then set -- --help; fi
    exec "${compose[@]}" -f compose.proxy-host.yml run --rm crawler "$@"
    ;;
  warehouse)
    if (( $# == 0 )); then set -- --help; fi
    exec "${compose[@]}" -f compose.proxy-host.yml run --rm --no-deps --entrypoint stock-warehouse crawler "$@"
    ;;
  native)
    [[ -x .venv-native/bin/stock-crawler ]] || {
      echo 'Install this project in .venv-native with Python 3.12; see docs/ubuntu-proxy.md.' >&2
      exit 2
    }
    export MSSQL_HOST=127.0.0.1 MSSQL_PORT="$CRAWLER_DB_HOST_PORT"
    export COMPANY_FILE="$PWD/config/companies.txt" DATA_DIR="$PWD/data"
    export KAP_COMPANY_REGISTRY="$PWD/config/kap_companies.json"
    export FIXTURE_SOURCE_DIR="$PWD/tests/fixtures/kap/source"
    export HTTP_PROXY="$CRAWLER_PROXY_URL" HTTPS_PROXY="$CRAWLER_PROXY_URL" ALL_PROXY="$CRAWLER_PROXY_URL"
    export http_proxy="$CRAWLER_PROXY_URL" https_proxy="$CRAWLER_PROXY_URL" all_proxy="$CRAWLER_PROXY_URL"
    export NO_PROXY='localhost,127.0.0.1,::1,mssql' no_proxy='localhost,127.0.0.1,::1,mssql'
    if (( $# == 0 )); then set -- --help; fi
    # Pydantic reads the existing .env for credentials. Do not source it as shell code.
    exec .venv-native/bin/stock-crawler "$@"
    ;;
  *)
    echo "Unknown mode: $mode. Use --help." >&2
    exit 2
    ;;
esac
