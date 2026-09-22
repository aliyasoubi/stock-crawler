# Warehouse extension — 2026-09-21

## 2026-09-22 — online daily market snapshot

- Simplified the local Docker workflow: warehouse commands now reuse `.env` by
  default, while `--env-file` remains an explicit external-database override.
- `warehouse init-db` automatically selects bootstrap credentials; other
  warehouse writes use the least-privilege `crawler_writer` account.
- Added `stock-warehouse isyatirim-daily` for batched, no-login latest-day
  `MarketData` and `MarketIndexData` acquisition.
- Mapped provider share quantity and actual TRY turnover separately; removed the
  unsafe `ClosePrice * Volume` turnover estimate from the price normalizer.
- Added an EOD safety gate, raw-response archive, source priority/provenance,
  missing-symbol quarantine and provider throttle stop.
- Added offline tests and live THYAO/XU100 plus 50-code pilot verification.
- Corrected the warehouse connection template filename.

Read `docs/WAREHOUSE-IMPLEMENTATION.md` first. Run commands from the project root.

Added:
- `src/stock_crawler/warehouse_fundamentals.py`: quarterly KAP complementary-file merge and mappings.
- `src/stock_crawler/warehouse.py`: seven target contracts, validation, staging and guarded promotion.
- `src/stock_crawler/warehouse_sources.py`: bounded source capture, EVDS replay, reviewed vendor CSV/JSON mapping.
- `src/stock_crawler/warehouse_cli.py`: separate client-warehouse CLI.
- `sql/warehouse-staging.sql`: append-only batch/observation tables; no target-table changes.
- `examples/warehouse/`: source metadata and deliberately unverified mapping templates.
- `tests/test_warehouse.py`: 25 tests; supplied workbooks are in `tests/fixtures/warehouse/`.
- `validation/ASELS-2025-warehouse.json`: actual sample output, with missing values left null.

Modified:
- `kap_export.read_export` accepts optional additional reviewed headers. Its default remains strict.
- Browser exporter accepts explicit periods and captured item IDs; resume identity includes periods.
- `pyproject.toml` adds `stock-warehouse` entrypoint.
- README links to the new implementation guide.

This package does not load an external database, install a scheduler, establish BIST
data entitlement or provide an EVDS key. Existing annual sync behavior is retained.
Original local credentials and generated caches are not included in this distribution.
