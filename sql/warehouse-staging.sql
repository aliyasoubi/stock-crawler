/* Run in the CLIENT warehouse database. No target tables or FactorStore are altered.
   Requires SQL Server 2016+ for ISJSON. Use a deployment account, not crawler_writer. */
SET XACT_ABORT ON;
BEGIN TRANSACTION;
IF SCHEMA_ID(N'stg') IS NULL EXEC(N'CREATE SCHEMA stg');
IF OBJECT_ID(N'stg.WarehouseBatch', N'U') IS NULL
CREATE TABLE stg.WarehouseBatch (
    BatchHash char(64) NOT NULL PRIMARY KEY,
    CapturedAt datetimeoffset NOT NULL,
    Payload nvarchar(max) NOT NULL CHECK (ISJSON(Payload) = 1)
);
IF OBJECT_ID(N'stg.WarehouseObservation', N'U') IS NULL
BEGIN
CREATE TABLE stg.WarehouseObservation (
    ObservationId bigint IDENTITY(1,1) NOT NULL PRIMARY KEY,
    BatchHash char(64) NOT NULL REFERENCES stg.WarehouseBatch(BatchHash),
    TargetTable varchar(32) NOT NULL,
    KeyHash char(64) NOT NULL,
    KeyJson nvarchar(500) NOT NULL CHECK (ISJSON(KeyJson) = 1),
    SourceJson nvarchar(max) NOT NULL CHECK (ISJSON(SourceJson) = 1),
    ValuesJson nvarchar(max) NOT NULL CHECK (ISJSON(ValuesJson) = 1),
    Status varchar(16) NOT NULL CHECK (Status IN ('inserted','updated','skipped','incomplete')),
    IssuesJson nvarchar(max) NOT NULL CHECK (ISJSON(IssuesJson) = 1),
    CONSTRAINT CK_WarehouseTarget CHECK (TargetTable IN
       ('Market','Company','CompanyFundamental','MarketData','MarketIndexMaster','MarketIndexData','MacroSovereign'))
);
CREATE INDEX IX_WarehouseObservation_Key
    ON stg.WarehouseObservation(TargetTable, KeyHash, ObservationId DESC) INCLUDE(Status);
END;
COMMIT;
