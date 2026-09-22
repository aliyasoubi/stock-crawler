/* Repeatable static seed. Run warehouse-schema.sql first if tables are missing.
   Supports existing IDENTITY and non-IDENTITY keys. Preserves all existing IDs.
   No company metadata or observations are fabricated. */
SET XACT_ABORT ON;
BEGIN TRANSACTION;
DECLARE @lock int;
EXEC @lock = sys.sp_getapplock @Resource=N'stock-crawler-warehouse',
 @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=10000;
IF @lock < 0 THROW 50001, 'Could not acquire warehouse lock', 1;
DECLARE @MarketId int;
IF (SELECT COUNT(*) FROM dbo.Market WITH (UPDLOCK,HOLDLOCK) WHERE MarketCode='BIST') > 1
 THROW 50002, 'Duplicate BIST markets: reconcile master data', 1;
SELECT @MarketId=MarketId FROM dbo.Market WITH (UPDLOCK,HOLDLOCK) WHERE MarketCode='BIST';
IF @MarketId IS NOT NULL AND EXISTS (
 SELECT 1 FROM dbo.Market WHERE MarketId=@MarketId AND
 (CountryCode IS NULL OR CountryCode<>'TR' OR BaseCurrency IS NULL OR BaseCurrency<>'TRY'))
 THROW 50003, 'BIST market conflicts with TR/TRY seed', 1;
IF @MarketId IS NULL
BEGIN
 IF COLUMNPROPERTY(OBJECT_ID('dbo.Market'),'MarketId','IsIdentity')=1
 BEGIN
  INSERT dbo.Market(MarketCode,CountryCode,CountryName,BaseCurrency) VALUES ('BIST','TR',N'Türkiye','TRY');
  SET @MarketId=CONVERT(int,SCOPE_IDENTITY());
 END
 ELSE
 BEGIN
  SELECT @MarketId=ISNULL(MAX(MarketId),0)+1 FROM dbo.Market WITH (UPDLOCK,HOLDLOCK);
  INSERT dbo.Market(MarketId,MarketCode,CountryCode,CountryName,BaseCurrency) VALUES (@MarketId,'BIST','TR',N'Türkiye','TRY');
 END;
END;
DECLARE @Seeds TABLE (IndexCode varchar(20),IndexName nvarchar(100));
INSERT @Seeds VALUES ('XU100',N'BIST 100'),('XU030',N'BIST 30'),('XU050',N'BIST 50'),('XUTUM',N'BIST All Shares');
IF EXISTS (SELECT IndexCode FROM dbo.MarketIndexMaster WITH (UPDLOCK,HOLDLOCK)
 WHERE MarketId=@MarketId GROUP BY IndexCode HAVING COUNT(*)>1)
 THROW 50004, 'Duplicate index codes: reconcile master data', 1;
IF COLUMNPROPERTY(OBJECT_ID('dbo.MarketIndexMaster'),'IndexId','IsIdentity')=1
 INSERT dbo.MarketIndexMaster(IndexCode,MarketId,IndexName)
 SELECT s.IndexCode,@MarketId,s.IndexName FROM @Seeds s
 WHERE NOT EXISTS (SELECT 1 FROM dbo.MarketIndexMaster i WITH (UPDLOCK,HOLDLOCK) WHERE i.MarketId=@MarketId AND i.IndexCode=s.IndexCode);
ELSE
BEGIN
 DECLARE @MaxId int;
 SELECT @MaxId=ISNULL(MAX(IndexId),0) FROM dbo.MarketIndexMaster WITH (UPDLOCK,HOLDLOCK);
 INSERT dbo.MarketIndexMaster(IndexId,IndexCode,MarketId,IndexName)
 SELECT @MaxId+ROW_NUMBER() OVER(ORDER BY s.IndexCode),s.IndexCode,@MarketId,s.IndexName FROM @Seeds s
 WHERE NOT EXISTS (SELECT 1 FROM dbo.MarketIndexMaster i WITH (UPDLOCK,HOLDLOCK) WHERE i.MarketId=@MarketId AND i.IndexCode=s.IndexCode);
END;
COMMIT;
SELECT * FROM dbo.Market WHERE MarketId=@MarketId;
SELECT * FROM dbo.MarketIndexMaster WHERE MarketId=@MarketId ORDER BY IndexId;
