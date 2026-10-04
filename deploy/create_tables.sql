-- The tables load_db fills (config/db_tables.csv), for a new, empty database. Run it once,
-- before the first load. It is all or nothing: if a table already exists, it stops and
-- creates none. Column types fit the values the crawlers write (README section 7).
--
-- With SQL Server in Docker (see README section 9):
--   docker cp deploy/create_tables.sql stock-crawler-mssql:/tmp/create_tables.sql
--   docker exec -e SQLCMDPASSWORD="$DB_PASSWORD" stock-crawler-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -C -b -d StockDb -i /tmp/create_tables.sql
SET XACT_ABORT ON;
BEGIN TRANSACTION;

CREATE TABLE dbo.Market (                       -- output/market_index/market.csv (config/markets.csv)
    MarketId     int          NOT NULL PRIMARY KEY,
    MarketCode   varchar(10)  NOT NULL,
    CountryCode  char(2)      NOT NULL,
    CountryName  nvarchar(50) NOT NULL,
    BaseCurrency char(3)      NOT NULL);

CREATE TABLE dbo.MarketIndexMaster (            -- output/market_index/market_index_master.csv
    IndexId   int           NOT NULL PRIMARY KEY,
    IndexCode varchar(20)   NOT NULL,
    MarketId  int           NOT NULL REFERENCES dbo.Market (MarketId),
    IndexName nvarchar(100) NOT NULL,
    UNIQUE (MarketId, IndexCode));

CREATE TABLE dbo.MarketIndexData (              -- output/market_index/market_index_data.csv
    TradeDate  date          NOT NULL,
    IndexId    int           NOT NULL REFERENCES dbo.MarketIndexMaster (IndexId),
    ClosePrice decimal(24,6) NULL,
    PRIMARY KEY (TradeDate, IndexId));

CREATE TABLE dbo.Company (                      -- output/companies/companies.csv
    CompanyId         int IDENTITY   NOT NULL PRIMARY KEY,  -- made here; MarketData gets it via config/company_ids.csv
    Ticker            varchar(32)    NOT NULL,
    MarketId          int            NOT NULL REFERENCES dbo.Market (MarketId),
    FullName          nvarchar(max)  NULL,
    SectorName        nvarchar(max)  NULL,
    ReportingCurrency nvarchar(100)  NULL,
    IsActive          bit            NOT NULL DEFAULT (1),
    IpoDate           date           NULL,
    IpoDateSource     nvarchar(100)  NULL,
    UNIQUE (MarketId, Ticker));

CREATE TABLE dbo.MarketData (                   -- output/market_data/market_data_db.csv (market_data --db-output)
    TradeDate      date          NOT NULL,
    CompanyId      int           NOT NULL REFERENCES dbo.Company (CompanyId),
    OpenPrice      decimal(18,4) NULL,
    HighPrice      decimal(18,4) NULL,
    LowPrice       decimal(18,4) NULL,
    ClosePrice     decimal(18,4) NULL,
    Volume         bigint        NULL,
    ValueTraded    decimal(24,4) NULL,
    SourcePriority tinyint       NOT NULL DEFAULT (1),
    PRIMARY KEY (TradeDate, CompanyId));

-- fundamentals identifies companies by KAP's company code (832, 833, ...), not by
-- dbo.Company's CompanyId. KapCompany lists those codes, so CompanyFundamental's foreign key
-- points at the right company. To reach dbo.Company, join KapCompany.StockCode to
-- Company.Ticker; a few StockCodes list two codes, e.g. "A1CAP, ACP".
CREATE TABLE dbo.KapCompany (                   -- output/fundamentals/companies.csv
    CompanyId    int           NOT NULL PRIMARY KEY,   -- KAP's company code
    StockCode    varchar(100)  NOT NULL,
    CompanyName  nvarchar(400) NULL,
    KapMemberOid char(32)      NULL,
    Sector       varchar(20)   NULL);

CREATE TABLE dbo.CompanyFundamental (           -- output/fundamentals/company_fundamental.csv
    CompanyId             int           NOT NULL REFERENCES dbo.KapCompany (CompanyId),
    FiscalYear            smallint      NOT NULL,
    FiscalQuarter         tinyint       NOT NULL CHECK (FiscalQuarter BETWEEN 1 AND 4),
    PeriodEndDate         date          NULL,
    PublishDate           date          NULL,
    Revenue               decimal(38,6) NULL,
    OperatingIncome       decimal(38,6) NULL,
    NetIncome             decimal(38,6) NULL,
    Ebitda                decimal(38,6) NULL,
    TotalAssets           decimal(38,6) NULL,
    TotalLiabilities      decimal(38,6) NULL,
    Equity                decimal(38,6) NULL,
    TotalDebtShort        decimal(38,6) NULL,
    TotalDebtLong         decimal(38,6) NULL,
    CashAndEquivalents    decimal(38,6) NULL,
    CurrentLiabilities    decimal(38,6) NULL,
    NonCurrentLiabilities decimal(38,6) NULL,
    FreeCashFlow          decimal(38,6) NULL,
    Eps                   decimal(38,10) NULL,
    SharesOutstanding     decimal(38,6) NULL,
    PresentationCurrency  nvarchar(100) NULL,    -- unit of the values: TL, 1000TL, USD, ...
    PRIMARY KEY (CompanyId, FiscalYear, FiscalQuarter));

CREATE TABLE dbo.MacroSovereign (               -- output/sovereign/macro_sovereign_tr.csv
    MarketId     int           NOT NULL REFERENCES dbo.Market (MarketId),
    AsOfDate     date          NOT NULL,
    PeriodType   varchar(10)   NOT NULL CHECK (PeriodType IN ('MONTHLY', 'QUARTERLY', 'ANNUAL')),
    PublishDate  date          NOT NULL,
    Gdp          decimal(24,4) NULL,
    TaxRevenue   decimal(24,4) NULL,
    PublicDebt   decimal(24,4) NULL,
    InterestRate decimal(8,4)  NULL,
    Cpi          decimal(10,4) NULL,
    FxRateUsd    decimal(14,6) NULL,
    CdsSpreadBps decimal(10,2) NULL,
    PRIMARY KEY (MarketId, AsOfDate, PeriodType, PublishDate));

COMMIT;
