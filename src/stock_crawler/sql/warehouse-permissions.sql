-- Object-level grants to existing crawler roles; no new users/passwords.
-- Existing Grafana/fundamentals_reader_role can SELECT these warehouse tables.
IF DATABASE_PRINCIPAL_ID(N'crawler_writer_role') IS NOT NULL
BEGIN
 GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.Market TO crawler_writer_role;
 GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.Company TO crawler_writer_role;
 GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.MarketIndexMaster TO crawler_writer_role;
 GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.CompanyFundamental TO crawler_writer_role;
 GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.MarketData TO crawler_writer_role;
 GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.MarketIndexData TO crawler_writer_role;
 GRANT SELECT, INSERT, UPDATE ON OBJECT::dbo.MacroSovereign TO crawler_writer_role;
 GRANT SELECT, INSERT ON OBJECT::stg.WarehouseBatch TO crawler_writer_role;
 GRANT SELECT, INSERT ON OBJECT::stg.WarehouseObservation TO crawler_writer_role;
END;
GO
IF DATABASE_PRINCIPAL_ID(N'fundamentals_reader_role') IS NOT NULL
BEGIN
 GRANT SELECT ON OBJECT::dbo.Market TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::dbo.Company TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::dbo.MarketIndexMaster TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::dbo.CompanyFundamental TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::dbo.MarketData TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::dbo.MarketIndexData TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::dbo.MacroSovereign TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::stg.WarehouseBatch TO fundamentals_reader_role;
 GRANT SELECT ON OBJECT::stg.WarehouseObservation TO fundamentals_reader_role;
END;
GO
