#!/usr/bin/env bash
# Restore a backup directory produced by scripts/backup.sh. Usage: scripts/restore.sh backups/<timestamp>
# Replaces the database and the raw/state directories; run init-db afterwards to recreate logins on a new server.
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:?usage: scripts/restore.sh backups/<timestamp>}"
env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
MSSQL_DATABASE="$(env_value MSSQL_DATABASE)"; MSSQL_SA_PASSWORD="$(env_value MSSQL_SA_PASSWORD)"
[[ "${MSSQL_DATABASE}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "Invalid database name"; exit 1; }
[ -n "${MSSQL_SA_PASSWORD}" ] || { echo "MSSQL_SA_PASSWORD missing in .env"; exit 1; }
mkdir -p data/state
exec 9>data/state/crawler.guard
flock -n 9 || { echo "Crawler or backup/restore is active; retry after it finishes."; exit 3; }
(cd "${SRC}" && sha256sum -c SHA256SUMS)

docker compose exec -T mssql mkdir -p /var/opt/mssql/backup
docker compose cp "${SRC}/${MSSQL_DATABASE}.bak" "mssql:/var/opt/mssql/backup/${MSSQL_DATABASE}.bak"
# compose cp writes as root; SQL Server runs as the mssql user
docker compose exec -T -u root mssql chown mssql:root "/var/opt/mssql/backup/${MSSQL_DATABASE}.bak"
docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -b -Q "
IF DB_ID(N'${MSSQL_DATABASE}') IS NOT NULL ALTER DATABASE [${MSSQL_DATABASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
RESTORE DATABASE [${MSSQL_DATABASE}] FROM DISK = N'/var/opt/mssql/backup/${MSSQL_DATABASE}.bak' WITH REPLACE, CHECKSUM;
ALTER DATABASE [${MSSQL_DATABASE}] SET MULTI_USER;"

# Keep the guard inode stable while the restore holds it.
rm -rf data/raw
find data/state -mindepth 1 -maxdepth 1 ! -name crawler.guard -exec rm -rf -- {} +
tar -xzf "${SRC}/raw-and-state.tgz" --exclude=state/crawler.guard --exclude=state/crawler.lock -C data
docker compose run --rm crawler init-db
echo "restored ${SRC}; verify with: docker compose run --rm crawler reprocess"
