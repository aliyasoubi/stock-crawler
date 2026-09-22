> **22 September update:** start with [warehouse-quickstart.md](warehouse-quickstart.md). It supersedes the setup/completeness instructions below: optional target creation, repeatable seeds, real ID exports, explicit partial financial loads and direct TCMB XML fetching are now available. The source-mapping details below still apply.

# Türkiye warehouse implementation

This extension adds a working review and staging pipeline for the seven tables in
your diagram, excluding FactorStore. The original annual crawler remains the entry
point `stock-crawler`; the new client-warehouse workflow is `stock-warehouse` or
`python -m stock_crawler.warehouse.cli`. Start with the sample command below.

**Implemented:** quarterly KAP parsing, complementary-file merging, explicit extended
concept mappings, source capture, configurable CSV/JSON vendor imports, EVDS parsing
and bounded fetch, reference seeds, SQL staging and guarded target loading.

**Not yet verified against your environment:** live KAP extended item IDs, BIST and
İş Yatırım response layouts/access, current EVDS series/profile/API key, and SQL Server
execution. No external database was populated. Missing facts remain null. Templates
are configuration starting points, not verified source definitions.

## 1. What the supplied workbooks actually contain

Both files contain ASELSAN, consolidated GENERAL statements, 2025 periods 1–4, in
`1000TL`. They refer to the same four notification IDs. They are complementary
selections of concepts from the same statements, not independent companies or
different statement revisions.

| Period | Notification | Revenue, TRY | OperatingIncome, TRY | NetIncome, TRY (total group) |
|---|---|---:|---:|---:|
| 2025 Q1 YTD | 1431115 | 22,790,773,000 | 7,406,479,000 | 2,133,491,000 |
| 2025 Q2 YTD | 1472637 | 53,710,197,000 | 17,458,086,000 | 6,410,609,000 |
| 2025 Q3 YTD | 1511313 | 90,872,870,000 | 26,273,798,000 | 11,587,759,000 |
| 2025 annual | 1561039 | 180,444,938,000 | 49,145,855,000 | 29,917,727,000 |

Eight of fifteen financial columns are available. The four period/publication
columns are also populated; PeriodEndDate is inferred only after calendar-year
confirmation. CompanyId must come from your database. Seven financial columns
remain absent, so none of these rows is ready for the strict final table.

| CompanyFundamental column | Mapping / source requirement |
|---|---|
| CompanyId | Explicit ticker → client CompanyId map; never KAP ID or array position |
| FiscalYear | Year |
| FiscalQuarter | Period 1–4; denotes YTD duration, including annual = 4 |
| PeriodEndDate | Calendar quarter end after explicit issuer-calendar confirmation |
| PublishDate | KAP publication timestamp converted to Istanbul date; full timestamp retained |
| Revenue | Revenue; finance-sector-only fallback is recorded explicitly |
| OperatingIncome | Profit (Loss) From Operating Activities — present in sample (3) |
| NetIncome | Net Profit (Loss), or owners-of-parent with the CLI option |
| TotalAssets | Total Assets |
| TotalLiabilities | Total Liabilities; otherwise CurrentLiabilities + NonCurrentLiabilities |
| Equity | Total Equity, including non-controlling interests |
| CurrentLiabilities | Current Liabilities |
| NonCurrentLiabilities | Non-current Liabilities |
| CashAndEquivalents | Additional cash concept needed; not net change in cash |
| TotalDebtShort | Reviewed current borrowing total, including current maturities; explicit lease policy |
| TotalDebtLong | Reviewed non-current borrowing total; same lease policy |
| Ebitda | Verified reported value, or operating income + matching operating D&A |
| FreeCashFlow | Operating cash flow minus positive cash purchases of PPE and intangibles |
| Eps | Explicit basic/diluted basis and per-share unit; not multiplied by monetary statement scale |
| SharesOutstanding | Verified period-end count and share/unit definition; not paid-in capital by default |

The extra Gross Profit and Cost of Sales columns are preserved in source evidence,
although your target table has no corresponding fields. The loader checks balance
sheet and liability identities, detects conflicting same-notification values, and
retains both workbook hashes. It does not join different notifications to fill gaps.

## 2. Recommended source for every other table

| Table / columns | Primary source | Implemented route and practical limit |
|---|---|---|
| Market: all fields | Small controlled reference seed | `reference --market-id ...`; choose actual warehouse ID |
| Company: Ticker, FullName | KAP registry reconciled to BIST instrument listing | `reference` produces review candidates; `vendor-csv` / `csv` load enriched rows |
| Company: MarketId | Your Market key | Explicit mapping, checked by database FK |
| Company: SectorName | BIST/KAP classification | Retain classification definition/date; do not infer from company name |
| Company: ReportingCurrency | Verified issuer financial-report currency | Never assume every company reports TRY or use market trading currency |
| Company: IsActive | Current exchange listing + effective delisting/relisting events | Do not infer inactive from a missing price/report or suspension |
| Company: IpoDate | Offering data or first-trading-date file, depending on agreed definition | First trading date is not necessarily legal IPO date |
| MarketData: dates, OHLC, Volume, ValueTraded | BIST licensed/accessible daily equity file | Verified vendor profile maps exact columns; check instrument/session filters and units |
| MarketData: SourcePriority | Controlled provider policy | Lower wins: BIST 1, verified İş Yatırım fallback 2, optional Yahoo cross-check 3 |
| MarketIndexMaster: all fields | Controlled list based on BIST official index definitions | XU100/XU030/XU050/XUTUM seeds; explicit database IDs |
| MarketIndexData: TradeDate, IndexId, ClosePrice | BIST index data / verified vendor index series | Same CSV/JSON importer with a separate index profile; do not assume equity file contains indices |
| MacroSovereign: MarketId | Your Market key | Supplied explicitly |
| MacroSovereign: AsOfDate, PeriodType | Native series observation period | D/M/Q/A; month/quarter/year stored at period end |
| MacroSovereign: PublishDate | Actual source release date | EVDS response used here does not establish it; leave null, never use retrieval date |
| FxRateUsd | TCMB EVDS | Define TRY per USD, buying vs selling; current series code must be checked |
| InterestRate | TCMB policy rate | Define one-week repo policy rate, percent rather than fraction; do not substitute TLREF |
| Cpi | TÜİK series through EVDS | Define index level and current base year; not YoY inflation rate |
| Gdp | TÜİK series through EVDS | Specify current-price TRY quarterly level; not growth %, real or annualized GDP |
| TaxRevenue | Treasury/Finance fiscal series via EVDS or source tables | Define central-government scope and monthly versus cumulative YTD amounts |
| PublicDebt | Treasury debt stock via EVDS or source tables | Define central/general government, gross/net and TRY conversion; not a flow |
| CdsSpreadBps | Licensed provider | Define Turkey sovereign, tenor (usually 5Y), contract/currency and timestamp; otherwise null |

For prices, **Volume means shares traded; ValueTraded means currency turnover**.
Do not map a vendor field labelled “volume” without checking its unit. Do not
calculate turnover as closing price × volume or synthesize open/high/low from close.
Raw/as-traded and adjusted price histories must not be mixed. Existing FactorStore
ownership is respected, but corporate-action adjustment still needs a separately
defined downstream contract; it cannot be assumed to happen automatically.

Keep historic ticker aliases with effective dates and stable issuer/instrument IDs.
Companies with multiple listed share classes need explicit modelling; changing a
ticker string is not enough. The registry is a discovery seed, not a confirmed
active-equity universe. No heuristic deletion of its 754 entries is implemented.

## 3. Run the supplied samples now

From the extracted project root, in your normal project virtual environment:

```bash
python -m pip install -e '.[dev]'
python -m stock_crawler.warehouse.cli fundamentals \
  --input tests/fixtures/warehouse/asels_2025_baseline.xlsx \
          tests/fixtures/warehouse/asels_2025_operating.xlsx \
  --calendar-tickers ASELS \
  --currency TRY \
  --output data/warehouse/ASELS-2025.json
```

Exit **1 is expected**: the output contains four useful but incomplete records.
It is not a parser failure. Seven financial fields and CompanyId are absent. The
same result, without fabricated IDs, is bundled at
`validation/ASELS-2025-warehouse.json`.

Create `config/client_company_ids.json` using the real database IDs, for example
an object whose key is `ASELS` and whose value is its actual integer CompanyId.
Re-run with `--company-map config/client_company_ids.json`. There are deliberately
no invented production IDs in the supplied sample result.

Use `--net-income-basis owners-of-parent` if that is your agreed target definition.
Annual 2025 owners' profit is TRY 29,949,517,000; total group profit is
TRY 29,917,727,000. Equity remains **total equity** under this contract, so the factor
team must choose consistent numerator/denominator definitions for ROE and valuation.

For Docker, rebuild and override the original entrypoint:

```bash
docker compose build crawler
docker compose run --rm --no-deps --entrypoint python crawler \
  -m stock_crawler.warehouse.cli --help
```

Put workbooks in your existing mounted imports folder and use their `/app/imports/...`
paths for Docker commands. The image does not copy bundled test fixtures.

## 4. Extend KAP acquisition without invented item IDs

1. On the English KAP item comparison page, choose one issuer, one period and the
   additional cash, borrowing, EPS and cash-flow concepts. Export once.
2. In browser Developer Tools → Network, save the JSON request body for
   `compareItems` as `config/kap-request.json`, including `itemIdList`. Save the XLSX.
3. Match each returned header/value to that notification's financial report and
   notes. Copy `examples/warehouse/kap-additional-items.template.json` to a real
   mapping file. Replace all placeholders. `item_id` is captured evidence, not a guess.
4. Test the one-company export with `fundamentals --mapping ...` before scaling up.
5. Generate the paced, resumable browser script using the verified request:

```bash
python -m stock_crawler.warehouse.cli kap-script \
  --company-file config/companies.txt --years 2025 \
  --periods 1,2,3,4 --request config/kap-request.json \
  --max-requests 5 --output data/warehouse/kap-quarterly.js
```

Run the generated script from the existing KAP browser workflow. It retains the
project's budget, batching and resume mechanism. Period selection is now part of
the resume identity. Pass downloaded XLSX files **or browser JSON manifests** to
the new `fundamentals` command. Do not feed extended/quarterly exports to the old
annual import command. Each invocation has a request cap; it is not a promise that
more periods and items will always fit the same source response size.

Additional mappings accept exact source headers with `target`, `unit`, captured
`item_id`, and `evidence`. Money uses statement scaling. EPS requires `unit=per_share`
and explicit scale. Shares require `unit=shares` and explicit scale. Debt, EPS,
shares, EBITDA and FCF mappings also require a written `definition`.

For derivation, supported input targets are `OperatingCashFlow`, `CapexCashOutflow`
and `OperatingDepreciationAmortization`. Capex is a **positive cash-outflow magnitude**;
negative source cash-flow lines need a reviewed source transformation first.
Operating D&A must be the expense included in that operating-income subtotal,
not an unrelated total cash-flow adjustment. These inputs must be for the same
notification/scope/YTD period. The adapter never guesses debt components, lease
inclusion or share counts; use a reviewed reported total or a dedicated extension
with non-overlapping components and tests.

Some requested facts may only exist in report notes, not the comparison exporter.
In that case capture the full KAP notification/issuer report and add a reviewed
adapter. Merely ticking more items cannot guarantee full coverage across sectors.
Bank/insurance formats remain quarantined; do not impose industrial EBITDA/FCF
definitions on them just to populate every cell.

## 5. Reference, BIST and İş Yatırım imports

Use `reference --market-id YOUR_ID --output ...` to prepare market/index seeds and
registry company candidates. Optional `--company-map` and `--index-map` supply actual
IDs. Company candidates are explicitly blocked until listing/currency/sector/date
metadata is enriched. The four index IDs are not invented.

Normalized CSV files use exactly the target column names shown in the diagram,
UTF-8, comma delimiters, ISO dates, and plain decimal-point numbers without grouping.
Empty cells mean unknown. For example:

```bash
python -m stock_crawler.warehouse.cli csv \
  --table MarketData --input imports/prices-normalized.csv \
  --source examples/warehouse/bist-source.json \
  --output data/warehouse/prices.json
```

For an unmodified BIST/vendor file, copy the BIST profile template, enter the actual
headers, encoding, delimiter, date format, identity lookup and unit conversions.
Filter to the required instrument class and daily aggregate session before setting
`verified=true`. Multiple conflicting records for one date/instrument are rejected.

```bash
python -m stock_crawler.warehouse.cli vendor-csv \
  --input imports/bist-daily.csv --profile config/bist-prices-profile.json \
  --output data/warehouse/prices.json
```

The same command accepts JSON when the profile says `format: "json"` and gives
`rows_path`, for example `["value"]` after verifying the actual İş Yatırım response.
Each `fields` entry reads an exact `column` (JSON key), a constant or an explicit
lookup. The importer supports decimal, Turkish and English-grouped numbers. Missing
OHLC fields stay absent; the record is staged as incomplete.

`fetch --url ... --output ...` captures exactly one reviewed HTTPS BIST/İş Yatırım
URL with a 25 MiB cap, source hash and metadata sidecar. It rejects redirects and
HTML responses, and stops on 429/503. Download authorized ZIP files manually and
extract the needed CSV before importing; compressed vendor archives are not parsed
automatically. For full-market backfill, schedule bounded company/date batches in
your existing scheduler after a real-file pilot. No unverified HisseTekil field
mapping, unattended bulk loop, or Yahoo production adapter is shipped.

## 6. EVDS macro data

TCMB announced EVDS 3 in January 2026. The old EVDS 2 guide links redirected to the
EVDS 3 site during this review, so the proposal's old hard-coded endpoint/series
must not be treated as a tested current contract. The supplied profile includes a
candidate EVDS 3 base URL and is deliberately **unverified**. Confirm it using your
current official API documentation and one successful response.

Copy `evds-profile.template.json`, replace the code and response key using the
current catalog, record the unit/definition/evidence and set `verified=true` only
after checking them. Use one native frequency per profile. The API key is read from
the `EVDS_API_KEY` environment variable and sent in an HTTP header.

```bash
python -m stock_crawler.warehouse.cli evds \
  --profile config/evds-fx.json --market-id YOUR_MARKET_ID \
  --fetch --start 2025-01-01 --end 2025-12-31 \
  --input imports/evds-fx.json --output data/warehouse/fx.json
```

To replay a saved JSON response, omit `--fetch`, `--start` and `--end`.
No forward-filling, forced common frequency or fabricated release date is performed.
Daily dates are `DD-MM-YYYY`, monthly `YYYY-M`, quarterly `YYYY-Qn`, annual `YYYY`;
an unfamiliar format fails visibly. EVDS empty observations remain missing.

Your current MacroSovereign design cannot retain different release dates for GDP,
CPI and tax revenue in one row. Store native-frequency observations with nullable
unrelated columns; preserve field-level source definitions in staging. If release
dates differ or are unknown, PublishDate is null. For rigorous historical factor
backtests, add an observation table keyed by series, observation period and vintage/
release timestamp. The supplied staging history preserves captures from now onward;
it cannot reconstruct historical availability that was never collected.

## 7. SQL Server staging and promotion

Run `sql/warehouse-staging.sql` in your **client warehouse database**. It creates
only `stg.WarehouseBatch` and `stg.WarehouseObservation`. It does not create, alter,
truncate or delete your seven target tables and never references FactorStore for writes.
Grant the loader SELECT/INSERT on these two staging tables and SELECT/INSERT/UPDATE
on only the target tables it needs. Deploy DDL with a separate deployment account.

The local Docker workflow automatically reuses the application's `.env`: bootstrap
credentials only for `warehouse init-db`, and `crawler_writer` for ordinary loads.
For a different external/client database, configure `config/client-warehouse.env`
with its `MSSQL_HOST`, `MSSQL_PORT`, `MSSQL_DATABASE`, `MSSQL_USER`,
`MSSQL_PASSWORD` and TLS settings, then pass it with `--env-file`. Keep credentials
out of source control.

First inspect the proposed load without opening SQL:

```bash
python -m stock_crawler.warehouse.cli load \
  --input data/warehouse/ASELS-2025.json
```

Then apply to your database when configured:

```bash
python -m stock_crawler.warehouse.cli load \
  --input data/warehouse/ASELS-2025.json \
  --apply \
  --output data/warehouse/load-summary.json
```

Add `--env-file config/client-warehouse.env` to that command only for the external
database override.

The sample is staged as incomplete, with **zero CompanyFundamental inserts**.
Missing metrics are not set to zero and target NOT NULL constraints are not loosened.
Ready records are checked against the actual target PK, columns and decimal types.
SQL enforces foreign keys. Load Market → Company/MarketIndexMaster → dependent facts.

If master IDs are IDENTITY columns, seed new master rows in the client's normal
master-data process and export the generated IDs. This loader does not turn on
IDENTITY_INSERT or invent IDs. Existing rows without this loader's provenance are
protected from overwrite, except a strictly better MarketData source priority.
Review those pre-existing rows explicitly before migrating ownership to this loader.

Promotion uses one serializable transaction and an application lock. Any SQL error
rolls back the batch. The original bundle hash makes reapplying the identical bundle
idempotent. Audit statuses are inserted/updated/skipped/incomplete. Same-provider
market data requires a newer capture; worse priority cannot replace better data.
Fundamentals use publication timestamp and notification ID, with currency/scope
checks; same-filing non-null conflicts do not overwrite approved values. This is a
batch-oriented implementation, not a million-row bulk loader: use moderate batches
and a set-based staging promotion extension if historical volume justifies it.

Dry-run `ready` means local content checks only; database completeness, permissions,
foreign keys, IDENTITY and other constraints are checked only during actual loading.
Source errors block apply; missing facts can still be staged. Exit codes: 0 success,
1 incomplete/needs review, 2 invalid input or operational error.

## 8. Corrections to the proposed approach

- Do not assume BIST files are all free direct downloads. Its official page lists
  the reference products but also directs data access to DataStore. Confirm current
  entitlement and file delivery before committing to a daily automatic downloader.
- Do not call annual minus 9M a reliable standalone Q4 for these Turkish statements.
  IAS 29 amounts can be in different purchasing-power units. Use compatible restated
  comparatives from the same reporting basis before differencing flows. Never
  difference balance-sheet stocks or EPS. The extension retains YTD and measuring dates.
- Paid-in capital ÷ assumed TRY 1 is not an authoritative share count. Nominal values,
  multiple classes, treasury shares and the required unit matter. EPS uses a weighted
  average denominator; period-end SharesOutstanding is a different quantity.
- EBITDA, FCF and debt require definitions; adding superficially similar taxonomy
  lines is not sufficient. Preserve reported zeros, missing values and not-applicable
  sector metrics as distinct states in your downstream design.
- The target three-column fundamental key loses source version, scope, currency
  and measuring-unit details. Keep the richer staging/provenance data alongside it.
- First trading date, legal IPO date, current listing status and trading suspension
  are different concepts. Reference reconciliation is a small recurring process,
  not a one-time heuristic filter.

## 9. Source references checked on 2026-09-21

- BIST equity/reference products and DataStore notices:
  https://www.borsaistanbul.com/en/data/equity-market-data
- BIST daily bulletin landing page:
  https://www.borsaistanbul.com/en/data/daily-bulletin
- BIST published reporting formats (version 1.3, 2017; not proof of current layout):
  https://www.borsaistanbul.com/files/borsa-istanbul-equity-data-and-index-data-reporting-and-acceptance-formats.pdf
- İş Yatırım company data page (not a stable public API contract):
  https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse=THYAO
- TCMB EVDS 3 announcement:
  https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/Duyurular/Basin/2026/DUY2026-03
- EVDS current portal: https://evds3.tcmb.gov.tr/
- IAS 33 share/EPS definition:
  https://www.ifrs.org/issued-standards/list-of-standards/ias-33-earnings-per-share/
- IAS 29 reporting/measuring units:
  https://www.ifrs.org/issued-standards/list-of-standards/ias-29-financial-reporting-in-hyperinflationary-economies/

These links establish source products and accounting definitions. They do not
substitute for a live authenticated endpoint/response test. See the included
validation summary for the tests actually run.
