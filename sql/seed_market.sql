-- Seed data for fixed reference tables
-- Market and MarketIndexMaster do not require crawling.

SET IDENTITY_INSERT Market ON;

INSERT INTO Market
(
    MarketId,
    MarketCode,
    CountryCode,
    CountryName,
    BaseCurrency
)
VALUES
(
    1,
    'BIST',
    'TR',
    N'Türkiye',
    'TRY'
);

SET IDENTITY_INSERT Market OFF;


SET IDENTITY_INSERT MarketIndexMaster ON;

INSERT INTO MarketIndexMaster
(
    IndexId,
    IndexCode,
    MarketId,
    IndexName
)
VALUES
(1, 'XU100', 1, N'BIST 100'),
(2, 'XU030', 1, N'BIST 30'),
(3, 'XU050', 1, N'BIST 50'),
(4, 'XUTUM', 1, N'BIST All Shares');

SET IDENTITY_INSERT MarketIndexMaster OFF;


-- Validation

SELECT *
FROM Market
WHERE MarketCode = 'BIST'
AND CountryCode = 'TR'
AND BaseCurrency = 'TRY';


SELECT *
FROM MarketIndexMaster
WHERE MarketId = 1
ORDER BY IndexId;
