#!/usr/bin/env bash
# Back up SQL Server (native .bak) plus raw snapshots and state into ./backups/<timestamp>/.
set -euo pipefail
cd "$(dirname "$0")/.."
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
MSSQL_DATABASE="$(env_value MSSQL_DATABASE)"; MSSQL_SA_PASSWORD="$(env_value MSSQL_SA_PASSWORD)"
[[ "${MSSQL_DATABASE}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "Invalid database name"; exit 1; }
[ -n "${MSSQL_SA_PASSWORD}" ] || { echo "MSSQL_SA_PASSWORD missing in .env"; exit 1; }
mkdir -p data/state
exec 9>data/state/crawler.guard
flock -n 9 || { echo "Crawler or backup/restore is active; retry after it finishes."; exit 3; }
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="backups/${STAMP}"
mkdir -p "${DEST}"

docker compose exec -T mssql mkdir -p /var/opt/mssql/backup
docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -b \
  -Q "BACKUP DATABASE [${MSSQL_DATABASE}] TO DISK = N'/var/opt/mssql/backup/${MSSQL_DATABASE}.bak' WITH INIT, CHECKSUM"
docker compose cp "mssql:/var/opt/mssql/backup/${MSSQL_DATABASE}.bak" "${DEST}/${MSSQL_DATABASE}.bak"
tar -czf "${DEST}/raw-and-state.tgz" --exclude=state/crawler.guard --exclude=state/crawler.lock -C data raw state
(cd "${DEST}" && sha256sum "${MSSQL_DATABASE}.bak" raw-and-state.tgz > SHA256SUMS)
echo "backup written to ${DEST}"
ls -la "${DEST}"
