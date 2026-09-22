/* Optional local warehouse schema. Run in your existing database.
   Same seven table/column names and numeric types as the supplied diagram.
   Unknown metadata and metrics are NULLABLE in NEW tables. Existing tables are
   NEVER altered. Use strict loading unless --allow-partial is explicitly chosen. */
SET XACT_ABORT ON;
GO
IF OBJECT_ID(N'dbo.Market', N'U') IS NULL
CREATE TABLE dbo.Market (
 MarketId int IDENTITY(1,1) NOT NULL PRIMARY KEY,
 MarketCode varchar(10) NOT NULL UNIQUE,
 CountryCode char(2) NOT NULL,
 CountryName nvarchar(50) NOT NULL,
 BaseCurrency char(3) NOT NULL
);
GO
IF OBJECT_ID(N'dbo.Company', N'U') IS NULL
CREATE TABLE dbo.Company (
 CompanyId int IDENTITY(1,1) NOT NULL PRIMARY KEY,
 Ticker varchar(32) NOT NULL,
 MarketId int NOT NULL REFERENCES dbo.Market(MarketId),
 FullName nvarchar(255) NOT NULL,
 SectorName nvarchar(100) NULL,
 ReportingCurrency char(3) NULL,
 IsActive bit NULL,
 IpoDate date NULL,
 UNIQUE (MarketId, Ticker)
);
GO
IF OBJECT_ID(N'dbo.MarketIndexMaster', N'U') IS NULL
CREATE TABLE dbo.MarketIndexMaster (
 IndexId int IDENTITY(1,1) NOT NULL PRIMARY KEY,
 IndexCode varchar(20) NOT NULL,
 MarketId int NOT NULL REFERENCES dbo.Market(MarketId),
 IndexName nvarchar(100) NOT NULL,
 UNIQUE (MarketId, IndexCode)
);
GO
IF OBJECT_ID(N'dbo.CompanyFundamental', N'U') IS NULL
CREATE TABLE dbo.CompanyFundamental (
 CompanyId int NOT NULL REFERENCES dbo.Company(CompanyId),
 FiscalYear smallint NOT NULL,
 FiscalQuarter tinyint NOT NULL CHECK (FiscalQuarter BETWEEN 1 AND 4),
 PeriodEndDate date NOT NULL,
 PublishDate date NOT NULL,
 Revenue decimal(22,4) NULL,
 OperatingIncome decimal(22,4) NULL,
 NetIncome decimal(22,4) NULL,
 Ebitda decimal(22,4) NULL,
 TotalAssets decimal(22,4) NULL,
 TotalLiabilities decimal(22,4) NULL,
 Equity decimal(22,4) NULL,
 TotalDebtShort decimal(22,4) NULL,
 TotalDebtLong decimal(22,4) NULL,
 CashAndEquivalents decimal(22,4) NULL,
 CurrentLiabilities decimal(22,4) NULL,
 NonCurrentLiabilities decimal(22,4) NULL,
 FreeCashFlow decimal(22,4) NULL,
 Eps decimal(14,4) NULL,
 SharesOutstanding decimal(22,2) NULL,
 PRIMARY KEY (CompanyId, FiscalYear, FiscalQuarter)
);
GO
IF OBJECT_ID(N'dbo.MarketData', N'U') IS NULL
CREATE TABLE dbo.MarketData (
 TradeDate date NOT NULL,
 CompanyId int NOT NULL REFERENCES dbo.Company(CompanyId),
 OpenPrice decimal(18,4) NULL,
 HighPrice decimal(18,4) NOT NULL,
 LowPrice decimal(18,4) NOT NULL,
 ClosePrice decimal(18,4) NOT NULL,
 Volume bigint NOT NULL,
 ValueTraded decimal(24,4) NOT NULL,
 SourcePriority tinyint NOT NULL,
 PRIMARY KEY (TradeDate, CompanyId)
);
GO
IF OBJECT_ID(N'dbo.MarketIndexData', N'U') IS NULL
CREATE TABLE dbo.MarketIndexData (
 TradeDate date NOT NULL,
 IndexId int NOT NULL REFERENCES dbo.MarketIndexMaster(IndexId),
 ClosePrice decimal(18,4) NOT NULL,
 PRIMARY KEY (TradeDate, IndexId)
);
GO
IF OBJECT_ID(N'dbo.MacroSovereign', N'U') IS NULL
CREATE TABLE dbo.MacroSovereign (
 MarketId int NOT NULL REFERENCES dbo.Market(MarketId),
 AsOfDate date NOT NULL,
 PeriodType varchar(10) NOT NULL CHECK (PeriodType IN ('D','M','Q','A')),
 PublishDate date NULL,
 Gdp decimal(24,4) NULL,
 TaxRevenue decimal(24,4) NULL,
 PublicDebt decimal(24,4) NULL,
 InterestRate decimal(8,4) NULL,
 Cpi decimal(10,4) NULL,
 FxRateUsd decimal(14,6) NULL,
 CdsSpreadBps decimal(10,2) NULL,
 PRIMARY KEY (MarketId, AsOfDate, PeriodType)
);
