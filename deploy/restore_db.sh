#!/usr/bin/env bash
# Restores a StockDb backup (.bak) into the SQL Server container and prints the row count of
# every table. Run it from the project folder:
#
#   export DB_PASSWORD='...'                 # the container's SA password
#   bash deploy/restore_db.sh                # backups/StockDb.bak into stock-crawler-mssql
#   bash deploy/restore_db.sh FILE.bak CONTAINER
#
# It checks the file against FILE.bak.sha256 if that exists, and asks before replacing a
# StockDb that already has data. The backup needs SQL Server 2022 or newer.
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP=${1:-backups/StockDb.bak}
CONTAINER=${2:-stock-crawler-mssql}
SQLCMD=/opt/mssql-tools18/bin/sqlcmd

fail() { echo "STOPPED: $*" >&2; exit 1; }
sql() {  # sql "T-SQL": run it as sa; the password reaches sqlcmd through the environment only
    SQLCMDPASSWORD="$DB_PASSWORD" docker exec -e SQLCMDPASSWORD "$CONTAINER" "$SQLCMD" \
        -S localhost -U sa -C -b -W "$@"
}

[ -n "${DB_PASSWORD:-}" ] || fail "set DB_PASSWORD to the container's SA password first"
[ -f "$BACKUP" ] || fail "no backup file at $BACKUP"
if [ -f "$BACKUP.sha256" ]; then
    expected=$(cut -d' ' -f1 "$BACKUP.sha256")
    actual=$( (sha256sum "$BACKUP" 2>/dev/null || shasum -a 256 "$BACKUP") | cut -d' ' -f1)
    [ "$expected" = "$actual" ] || fail "$BACKUP is damaged (checksum differs): copy it again"
    echo "Checksum OK"
fi
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" \
    || fail "container $CONTAINER is not running (docker ps -a; docker start $CONTAINER; docker logs $CONTAINER)"
sql -h -1 -Q "SELECT 1" >/dev/null || fail "cannot log in as sa: is DB_PASSWORD right? (docker logs $CONTAINER)"

rows=$(sql -h -1 -Q "SET NOCOUNT ON; IF DB_ID('StockDb') IS NULL SELECT 0 ELSE
    SELECT COUNT_BIG(*) FROM StockDb.sys.partitions p JOIN StockDb.sys.tables t ON t.object_id = p.object_id
    WHERE p.index_id IN (0, 1) AND p.rows > 0" | tr -d '[:space:]')
if [ "$rows" != "0" ]; then
    read -rp "StockDb already has data in $rows tables. Replace it with $BACKUP? Type yes: " answer
    [ "$answer" = "yes" ] || fail "nothing changed"
fi

echo "Restoring $BACKUP into StockDb ..."
docker cp "$BACKUP" "$CONTAINER:/tmp/StockDb.bak"
docker exec -u 0 "$CONTAINER" chmod 644 /tmp/StockDb.bak  # docker cp keeps the host's owner; SQL Server must read it
sql -Q "SET NOCOUNT ON;
    IF DB_ID('StockDb') IS NOT NULL ALTER DATABASE StockDb SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    BEGIN TRY
        RESTORE DATABASE StockDb FROM DISK = '/tmp/StockDb.bak' WITH REPLACE, CHECKSUM,
            MOVE 'StockDb' TO '/var/opt/mssql/data/StockDb.mdf',
            MOVE 'StockDb_log' TO '/var/opt/mssql/data/StockDb_log.ldf';
    END TRY
    BEGIN CATCH  -- leave the old StockDb usable, then report the error
        IF DB_ID('StockDb') IS NOT NULL ALTER DATABASE StockDb SET MULTI_USER;
        THROW;
    END CATCH;
    ALTER DATABASE StockDb SET MULTI_USER;" \
    || { docker exec -u 0 "$CONTAINER" rm -f /tmp/StockDb.bak; fail "restore failed (message above); StockDb is as it was"; }
docker exec -u 0 "$CONTAINER" rm -f /tmp/StockDb.bak

echo
sql -d StockDb -s " " -Q "SET NOCOUNT ON; SELECT t.name AS [Table], SUM(p.rows) AS [Rows]
    FROM sys.tables t JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
    GROUP BY t.name ORDER BY t.name"
echo
echo "Done: StockDb is restored."
