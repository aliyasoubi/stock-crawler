# Warehouse review and implementation — 22 September 2026

Start with `docs/warehouse-quickstart.md` for executable commands.

## Findings and changes

1. **Target tables were not created by either workflow.** The annual crawler's
   `init-db` creates its own tables, while the old warehouse loader assumed all
   seven client tables existed. Added a separate warehouse `init-db` to create
   missing targets/staging and grant existing application roles access.
2. **Static seed was unsafe to repeat.** It forced IDs 1–4 through IDENTITY_INSERT.
   Replaced it with a repeatable natural-key seed. Added `seed-reference` with
   IDENTITY/non-IDENTITY support, existing-ID preservation and optional selected
   company candidates. Neither path forces MarketId=1.
3. **No convenient ID mapping workflow.** Added `export-maps` for actual SQL IDs
   and an editable company CSV. Existing CSV edits survive map refreshes.
4. **Warehouse config could point at the main crawler DB unexpectedly.** Pydantic
   process variables outranked `--env-file`. Explicit warehouse files now own all
   database settings; required connection values must be present in that file.
5. **Partial financial rows were always blocked.** Added explicit
   `load --allow-partial`; missing metrics remain NULL and SQL constraints are
   still enforced. Strict mode is unchanged. New tables allow unknown metrics;
   existing tables are not altered. Same-filing enrichment preserves known facts;
   a newer filing replaces the financial snapshot instead of mixing filings.
6. **Repeated incomplete batches could report misleading success.** Replays now
   return the recorded incomplete statuses. Missing master descriptions/keys are
   also reported before SQL loading.
7. **Seeded companies could not subsequently be enriched.** Unknown metadata can
   be filled when existing facts agree; conflicting pre-existing master facts
   remain protected. Fundamental currency must agree with known company currency.
8. **Non-KAP acquisition was mostly generic adapters.** Added one small direct
   adapter for official TCMB indicative USD ForexBuying XML. Added a batched,
   no-login İş Yatırım latest-day adapter for equity OHLC/share quantity/actual
   TRY turnover and index close. It refuses today's intraday data by default and
   retains raw responses. Existing CSV/vendor and EVDS adapters remain in the
   same project. No new service architecture.
9. **SQL access/run flow was unclear.** Added the Ubuntu `warehouse` wrapper,
   connection example, table queries and a step-by-step guide.

## Verification performed

- Python regression suite: **237 passed, 2 skipped**, 7.85 seconds.
- Skips: legacy SQL integration and new warehouse SQL integration; no SQL Server
  test URL was supplied. **Actual SQL Server DDL/loading was not executed here.**
- New offline coverage: connection isolation, repeatable reference inserts for
  both key styles, strict metadata rejection, partial-fundamental validation,
  master enrichment protection, TCMB input/date/value checks, dry-run CLI.
- Reference insert tests use SQLite for insert behavior; they do not substitute
  for SQL Server transaction/locking/IDENTITY/permission verification.
- Real HTTP smoke: TCMB historical XML for 2025-09-19 returned HTTP 200,
  application/xml, 9,085 bytes. Parsed USD indicative buying rate 41.234400 TRY
  per USD and source date 2025-09-19. Original response saved as a test fixture.
- Bash syntax check and warehouse CLI help passed.
- An opt-in SQL Server lifecycle test is supplied in
  `tests/test_warehouse_sqlserver.py`; it requires an EMPTY disposable database
  and checks setup, repeatable seeds, mapping export, staging, partial loads,
  same-filing enrichment, restatements and replay statuses.

## Remaining inputs, not completed live integrations

- Licensed/official historical equity OHLC/share-volume/turnover and index-close
  files remain needed for backfill. The public İş Yatırım adapter covers only the
  latest daily snapshot and is a fallback source, not an official BIST license.
- Verified source facts for sector/currency/listing status/IPO date.
- Additional KAP concepts for EBITDA, split debt, cash, FCF, EPS and share count.
- EVDS key/current profile for additional macro series, and a CDS provider.
- SQL Server execution in the user's environment, using the included commands.

No external SQL database was populated during this review. FactorStore remains
excluded. The returned ZIP omits credentials and generated cache/build files;
retain your own environment/data files when updating your existing checkout.
