-- Restore StockDb.bak on SQL Server 2019 or newer (Windows or Linux), e.g. in SSMS:
--
--   1. Copy StockDb.bak into SQL Server's backup folder. To see where it is, run:
--        SELECT SERVERPROPERTY('InstanceDefaultBackupPath')
--      (usually C:\Program Files\Microsoft SQL Server\MSSQL15.<instance>\MSSQL\Backup).
--      SQL Server cannot read files in your own folders, e.g. Downloads or Desktop.
--   2. Open this file in SSMS, connected to the server, and press Execute (F5).
--   3. Compare the row counts it shows with RESTORE_ON_WINDOWS.txt next to the backup.
--
-- The database files go to SQL Server's default data and log folders. If StockDb already
-- exists it stops; set @replace = 1 to replace it (its current data is lost).
USE master;  -- not StockDb itself, or the restore finds it in use
SET NOCOUNT ON;
DECLARE @backup nvarchar(500) = NULL;   -- NULL: StockDb.bak in the backup folder; or a full path
DECLARE @replace bit = 0;

DECLARE @backup_folder nvarchar(400) = CONVERT(nvarchar(400), SERVERPROPERTY('InstanceDefaultBackupPath'));
DECLARE @data_folder nvarchar(400) = CONVERT(nvarchar(400), SERVERPROPERTY('InstanceDefaultDataPath'));
DECLARE @log_folder nvarchar(400) = CONVERT(nvarchar(400), SERVERPROPERTY('InstanceDefaultLogPath'));
DECLARE @sep nchar(1) = CASE WHEN @data_folder LIKE N'%/%' THEN N'/' ELSE N'\' END;  -- Linux or Windows
IF RIGHT(@backup_folder, 1) NOT IN (N'/', N'\') SET @backup_folder += @sep;
IF RIGHT(@data_folder, 1) NOT IN (N'/', N'\') SET @data_folder += @sep;
IF RIGHT(@log_folder, 1) NOT IN (N'/', N'\') SET @log_folder += @sep;
SET @backup = COALESCE(@backup, @backup_folder + N'StockDb.bak');
IF @backup IS NULL
    THROW 50000, N'This server reports no backup folder: set @backup to the full path of StockDb.bak.', 1;
DECLARE @data nvarchar(500) = @data_folder + N'StockDb.mdf';
DECLARE @log nvarchar(500) = @log_folder + N'StockDb_log.ldf';

PRINT N'Backup file: ' + @backup;
PRINT N'Data file:   ' + @data;
PRINT N'Log file:    ' + @log;
IF DB_ID(N'StockDb') IS NOT NULL AND @replace = 0
    THROW 50000, N'StockDb already exists, nothing was changed. Set @replace = 1 to replace it.', 1;

IF DB_ID(N'StockDb') IS NOT NULL
    ALTER DATABASE StockDb SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
RESTORE DATABASE StockDb FROM DISK = @backup
    WITH CHECKSUM, REPLACE, STATS = 25, MOVE N'StockDb' TO @data, MOVE N'StockDb_log' TO @log;
ALTER DATABASE StockDb SET MULTI_USER;
PRINT N'StockDb restored.';
GO

-- Row count of every table (after an error above, these are of the StockDb that was already there).
SELECT t.name AS [Table], SUM(p.rows) AS [Rows]
FROM StockDb.sys.tables AS t
JOIN StockDb.sys.partitions AS p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
GROUP BY t.name
ORDER BY t.name;
