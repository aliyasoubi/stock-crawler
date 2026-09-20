-- Stock Fundamental Crawler: initial schema (idempotent).
-- Executed batch-by-batch (split on lines containing only GO) by `stock-crawler init-db`
-- inside the target database. Logins/users are created by init-db, roles and grants live here.

IF OBJECT_ID('dbo.companies', 'U') IS NULL
CREATE TABLE dbo.companies (
    company_id                          INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_companies PRIMARY KEY,
    market_source                       VARCHAR(16)       NOT NULL,
    source_company_id                   VARCHAR(64)       NOT NULL,
    ticker                              VARCHAR(16)       NOT NULL,
    yahoo_ticker                        VARCHAR(24)       NULL,
    company_name                        NVARCHAR(256)     NULL,
    last_discovery_at                   DATETIMEOFFSET(3) NULL,
    last_success_at                     DATETIMEOFFSET(3) NULL,
    last_error                          NVARCHAR(1000)    NULL,
    latest_discovered_notification_id   VARCHAR(32)       NULL,
    created_at                          DATETIMEOFFSET(3) NOT NULL CONSTRAINT DF_companies_created_at DEFAULT SYSDATETIMEOFFSET(),
    updated_at                          DATETIMEOFFSET(3) NOT NULL CONSTRAINT DF_companies_updated_at DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT UQ_companies_source UNIQUE (market_source, source_company_id),
    CONSTRAINT UQ_companies_ticker UNIQUE (market_source, ticker)
);
GO

IF OBJECT_ID('dbo.reports', 'U') IS NULL
CREATE TABLE dbo.reports (
    report_id                 INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_reports PRIMARY KEY,
    company_id                INT               NOT NULL CONSTRAINT FK_reports_company REFERENCES dbo.companies(company_id),
    market_source             VARCHAR(16)       NOT NULL,
    notification_id           VARCHAR(32)       NOT NULL,
    published_at              DATETIMEOFFSET(3) NOT NULL,
    filing_fiscal_year        SMALLINT          NOT NULL,
    filing_fiscal_period      TINYINT           NOT NULL,
    filing_period_start_date  DATE              NULL,
    filing_period_end_date    DATE              NOT NULL,
    consolidation_scope       VARCHAR(16)       NOT NULL CONSTRAINT CK_reports_scope CHECK (consolidation_scope IN ('consolidated', 'unconsolidated')),
    statement_type            VARCHAR(32)       NOT NULL,
    source_url                NVARCHAR(1000)    NULL,
    document_url              NVARCHAR(1000)    NULL,
    raw_path                  NVARCHAR(500)     NOT NULL,
    content_hash              CHAR(64)          NOT NULL,
    parser_version            VARCHAR(16)       NOT NULL,
    retrieved_at              DATETIMEOFFSET(3) NOT NULL,
    parsed_at                 DATETIMEOFFSET(3) NULL,
    parse_status              VARCHAR(16)       NOT NULL CONSTRAINT CK_reports_status CHECK (parse_status IN ('valid', 'failed', 'unsupported')),
    is_withdrawn              BIT               NOT NULL CONSTRAINT DF_reports_is_withdrawn DEFAULT 0,
    validation_summary        NVARCHAR(MAX)     NULL,
    created_at                DATETIMEOFFSET(3) NOT NULL CONSTRAINT DF_reports_created_at DEFAULT SYSDATETIMEOFFSET(),
    CONSTRAINT UQ_reports_version UNIQUE (market_source, notification_id, content_hash, parser_version)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_reports_company_period' AND object_id = OBJECT_ID('dbo.reports'))
CREATE INDEX IX_reports_company_period ON dbo.reports (company_id, filing_fiscal_year, consolidation_scope, parse_status);
GO

IF OBJECT_ID('dbo.fundamentals', 'U') IS NULL
CREATE TABLE dbo.fundamentals (
    fundamental_id                                   INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_fundamentals PRIMARY KEY,
    report_id                                        INT           NOT NULL CONSTRAINT FK_fundamentals_report REFERENCES dbo.reports(report_id),
    fiscal_year                                      SMALLINT      NOT NULL,
    fiscal_period                                    TINYINT       NOT NULL,
    period_start_date                                DATE          NULL,
    period_end_date                                  DATE          NOT NULL,
    is_comparative                                   BIT           NOT NULL,
    currency_code                                    CHAR(3)       NOT NULL,
    currency_scale                                   BIGINT        NOT NULL,
    presentation_currency_raw                        NVARCHAR(32)  NOT NULL,
    -- IAS 29 / TMS 29 purchasing power the amounts are expressed in. For a current column this
    -- equals period_end_date; for a comparative it is the LATER filing's period end, because
    -- comparatives are restated into the current period's measuring unit. Amounts with
    -- different measuring_unit_date values are NOT directly comparable without a CPI bridge.
    measuring_unit_date                              DATE          NULL,
    -- Ten baseline concepts. Monetary values are normalized to base currency units (already scaled).
    total_liabilities_and_equity                     DECIMAL(38,6) NULL,
    profit_attributable_to_non_controlling_interests DECIMAL(38,6) NULL,
    profit_attributable_to_owners_of_parent          DECIMAL(38,6) NULL,
    current_liabilities                              DECIMAL(38,6) NULL,
    non_current_liabilities                          DECIMAL(38,6) NULL,
    total_equity                                     DECIMAL(38,6) NULL,
    total_assets                                     DECIMAL(38,6) NULL,
    revenue                                          DECIMAL(38,6) NULL,
    net_profit                                       DECIMAL(38,6) NULL,
    finance_sector_revenue                           DECIMAL(38,6) NULL,
    -- Seven additional concepts. eps is currency per share; shares_outstanding is a share count.
    eps                                              DECIMAL(38,6) NULL,
    operating_income                                 DECIMAL(38,6) NULL,
    cash_and_cash_equivalents                        DECIMAL(38,6) NULL,
    total_debt                                       DECIMAL(38,6) NULL,
    ebitda                                           DECIMAL(38,6) NULL,
    free_cash_flow                                   DECIMAL(38,6) NULL,
    shares_outstanding                               DECIMAL(38,6) NULL,
    total_debt_method                                VARCHAR(40)   NULL,
    ebitda_method                                    VARCHAR(40)   NULL,
    free_cash_flow_method                            VARCHAR(40)   NULL,
    shares_outstanding_method                        VARCHAR(40)   NULL,
    CONSTRAINT UQ_fundamentals_period UNIQUE (report_id, fiscal_year, fiscal_period)
);
GO

IF COL_LENGTH('dbo.fundamentals', 'measuring_unit_date') IS NULL
ALTER TABLE dbo.fundamentals ADD measuring_unit_date DATE NULL;
GO

-- Backfill current columns only. Existing comparative rows retain NULL until an offline
-- reprocess with parser 1.1.1 derives their measuring unit from the enclosing filing.
UPDATE dbo.fundamentals SET measuring_unit_date = period_end_date
WHERE measuring_unit_date IS NULL AND is_comparative = 0;
GO

-- Current-period rows only: one latest valid version per company / fiscal year / period / scope.
-- Version order: publication time, numeric notification id, capture time, parse time, report id.
-- Withdrawn notifications are excluded in every version.
CREATE OR ALTER VIEW dbo.vw_fundamentals AS
WITH ranked AS (
    SELECT
        r.report_id, r.company_id, r.market_source, r.notification_id, r.published_at,
        r.consolidation_scope, r.statement_type, r.source_url, r.document_url,
        r.content_hash, r.parser_version, r.retrieved_at, r.parsed_at, r.validation_summary,
        f.fundamental_id, f.fiscal_year, f.fiscal_period, f.period_start_date, f.period_end_date,
        f.currency_code, f.currency_scale, f.presentation_currency_raw, f.measuring_unit_date,
        f.total_liabilities_and_equity, f.profit_attributable_to_non_controlling_interests,
        f.profit_attributable_to_owners_of_parent, f.current_liabilities, f.non_current_liabilities,
        f.total_equity, f.total_assets, f.revenue, f.net_profit, f.finance_sector_revenue,
        f.eps, f.operating_income, f.cash_and_cash_equivalents, f.total_debt, f.ebitda,
        f.free_cash_flow, f.shares_outstanding,
        f.total_debt_method, f.ebitda_method, f.free_cash_flow_method, f.shares_outstanding_method,
        ROW_NUMBER() OVER (
            PARTITION BY r.company_id, f.fiscal_year, f.fiscal_period, r.consolidation_scope
            ORDER BY r.published_at DESC,
                     TRY_CONVERT(BIGINT, r.notification_id) DESC,
                     r.notification_id DESC,
                     r.retrieved_at DESC,
                     r.parsed_at DESC,
                     r.report_id DESC
        ) AS version_rank
    FROM dbo.reports r
    JOIN dbo.fundamentals f ON f.report_id = r.report_id
    WHERE r.parse_status = 'valid'
      AND r.is_withdrawn = 0
      AND f.is_comparative = 0
)
SELECT
    c.company_id, c.ticker, c.yahoo_ticker, c.company_name,
    c.last_discovery_at, c.last_success_at, c.last_error, c.latest_discovered_notification_id,
    k.report_id, k.market_source, k.notification_id, k.published_at, k.consolidation_scope, k.statement_type,
    k.source_url, k.document_url, k.content_hash, k.parser_version, k.retrieved_at, k.parsed_at,
    k.fiscal_year, k.fiscal_period, k.period_start_date, k.period_end_date,
    k.currency_code, k.currency_scale, k.presentation_currency_raw, k.measuring_unit_date,
    k.total_liabilities_and_equity, k.profit_attributable_to_non_controlling_interests,
    k.profit_attributable_to_owners_of_parent, k.current_liabilities, k.non_current_liabilities,
    k.total_equity, k.total_assets, k.revenue, k.net_profit, k.finance_sector_revenue,
    k.eps, k.operating_income, k.cash_and_cash_equivalents, k.total_debt, k.ebitda,
    k.free_cash_flow, k.shares_outstanding,
    k.total_debt_method, k.ebitda_method, k.free_cash_flow_method, k.shares_outstanding_method,
    k.validation_summary
FROM ranked k
JOIN dbo.companies c ON c.company_id = k.company_id
WHERE k.version_rank = 1;
GO

-- One latest annual row per company: latest period end, then consolidated before unconsolidated.
CREATE OR ALTER VIEW dbo.vw_latest_fundamentals AS
WITH ranked AS (
    SELECT v.*,
        ROW_NUMBER() OVER (
            PARTITION BY v.company_id
            ORDER BY v.period_end_date DESC,
                     CASE v.consolidation_scope WHEN 'consolidated' THEN 0 ELSE 1 END,
                     v.report_id DESC
        ) AS latest_rank
    FROM dbo.vw_fundamentals v
)
SELECT
    company_id, ticker, yahoo_ticker, company_name,
    last_discovery_at, last_success_at, last_error, latest_discovered_notification_id,
    report_id, market_source, notification_id, published_at, consolidation_scope, statement_type,
    source_url, document_url, content_hash, parser_version, retrieved_at, parsed_at,
    fiscal_year, fiscal_period, period_start_date, period_end_date,
    currency_code, currency_scale, presentation_currency_raw, measuring_unit_date,
    total_liabilities_and_equity, profit_attributable_to_non_controlling_interests,
    profit_attributable_to_owners_of_parent, current_liabilities, non_current_liabilities,
    total_equity, total_assets, revenue, net_profit, finance_sector_revenue,
    eps, operating_income, cash_and_cash_equivalents, total_debt, ebitda,
    free_cash_flow, shares_outstanding,
    total_debt_method, ebitda_method, free_cash_flow_method, shares_outstanding_method,
    validation_summary
FROM ranked
WHERE latest_rank = 1;
GO

-- Least-privilege roles. init-db maps logins/users onto these roles.
IF DATABASE_PRINCIPAL_ID('crawler_writer_role') IS NULL CREATE ROLE crawler_writer_role;
IF DATABASE_PRINCIPAL_ID('fundamentals_reader_role') IS NULL CREATE ROLE fundamentals_reader_role;
GO

GRANT SELECT, INSERT, UPDATE ON dbo.companies    TO crawler_writer_role;
GRANT SELECT, INSERT, UPDATE ON dbo.reports      TO crawler_writer_role;
GRANT SELECT, INSERT         ON dbo.fundamentals TO crawler_writer_role;
GRANT SELECT ON dbo.vw_fundamentals        TO crawler_writer_role;
GRANT SELECT ON dbo.vw_latest_fundamentals TO crawler_writer_role;
GRANT SELECT ON dbo.vw_fundamentals        TO fundamentals_reader_role;
GRANT SELECT ON dbo.vw_latest_fundamentals TO fundamentals_reader_role;
GO

-- Schema version marker checked by init-db before applying this file again.
IF OBJECT_ID('dbo.schema_migrations', 'U') IS NULL
CREATE TABLE dbo.schema_migrations (
    version    INT               NOT NULL CONSTRAINT PK_schema_migrations PRIMARY KEY,
    applied_at DATETIMEOFFSET(3) NOT NULL CONSTRAINT DF_schema_migrations_applied_at DEFAULT SYSDATETIMEOFFSET()
);
IF NOT EXISTS (SELECT 1 FROM dbo.schema_migrations WHERE version = 1)
INSERT INTO dbo.schema_migrations (version) VALUES (1);
IF NOT EXISTS (SELECT 1 FROM dbo.schema_migrations WHERE version = 2)
INSERT INTO dbo.schema_migrations (version) VALUES (2);
GO
