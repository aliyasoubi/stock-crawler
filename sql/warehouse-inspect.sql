-- Select your warehouse database in SSMS first. Run ONE query per Grafana panel.
-- Grafana: Explore -> StockFundamentals -> Code -> Format: Table.
SELECT DB_NAME() AS CurrentDatabase;
GO
SELECT s.name AS SchemaName, t.name AS TableName
FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
WHERE t.name IN ('Market','Company','CompanyFundamental','MarketData',
 'MarketIndexMaster','MarketIndexData','MacroSovereign','WarehouseBatch','WarehouseObservation')
ORDER BY s.name,t.name;
GO
SELECT 'Market' AS TableName, COUNT_BIG(*) AS RowCount FROM dbo.Market
UNION ALL SELECT 'Company', COUNT_BIG(*) FROM dbo.Company
UNION ALL SELECT 'CompanyFundamental', COUNT_BIG(*) FROM dbo.CompanyFundamental
UNION ALL SELECT 'MarketData', COUNT_BIG(*) FROM dbo.MarketData
UNION ALL SELECT 'MarketIndexMaster', COUNT_BIG(*) FROM dbo.MarketIndexMaster
UNION ALL SELECT 'MarketIndexData', COUNT_BIG(*) FROM dbo.MarketIndexData
UNION ALL SELECT 'MacroSovereign', COUNT_BIG(*) FROM dbo.MacroSovereign;
GO
SELECT * FROM dbo.Market;
GO
SELECT * FROM dbo.MarketIndexMaster ORDER BY IndexCode;
GO
SELECT CompanyId,Ticker,FullName,SectorName,ReportingCurrency,IsActive,IpoDate
FROM dbo.Company ORDER BY Ticker;
GO
SELECT TOP (100) c.Ticker, f.* FROM dbo.CompanyFundamental f
JOIN dbo.Company c ON c.CompanyId=f.CompanyId
ORDER BY f.FiscalYear DESC, f.FiscalQuarter DESC, c.Ticker;
GO
SELECT TOP (100) c.Ticker,p.* FROM dbo.MarketData p
JOIN dbo.Company c ON c.CompanyId=p.CompanyId ORDER BY p.TradeDate DESC,c.Ticker;
GO
SELECT TOP (100) i.IndexCode,p.* FROM dbo.MarketIndexData p
JOIN dbo.MarketIndexMaster i ON i.IndexId=p.IndexId ORDER BY p.TradeDate DESC,i.IndexCode;
GO
SELECT TOP (100) * FROM dbo.MacroSovereign ORDER BY AsOfDate DESC,PeriodType;
GO
-- Why an input did not reach a target table (unknown fields, schema constraints, etc.)
SELECT TOP (100) TargetTable,Status,KeyJson,IssuesJson
FROM stg.WarehouseObservation WHERE Status IN ('incomplete','skipped') ORDER BY ObservationId DESC;
GO
-- Available raw normalized facts remain readable even if a strict target rejects them.
SELECT TOP (100) KeyJson, ValuesJson, SourceJson
FROM stg.WarehouseObservation WHERE TargetTable='CompanyFundamental'
ORDER BY ObservationId DESC;
