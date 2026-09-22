# Client CompanyFundamental contract: implemented export and source gaps

## Result

An offline `export-company-fundamentals` command now reads the saved KAP comparison
workbooks and produces JSON with all 20 column names from the client's table.
It includes exact decimal strings, provenance, field availability, and validation
issues. It performs no HTTP requests and no SQL writes.

This is a **review export**, not a complete loader into `dbo.CompanyFundamental`.
The actual downloaded workbook contains only ten financial concepts. Eight of the
client's required financial fields are absent. The supplied `NOT NULL` contract
therefore cannot be populated honestly from that file alone.

The inspected archive contains four downloaded records: ASELS and THYAO, fiscal
years 2024 and 2025. Source workbook SHA-256:
`23e881694ffe14ec7190914fd4b9892b5e1c222ef1d150b4d946b09564b2b090`.
The separate test fixture has years 2023–2024 and is not substituted for this data.

## Exact mapping

| Client column | Current mapping | Availability / interpretation |
|---|---|---|
| CompanyId | Explicit ticker-to-client-ID JSON | Must come from the client's `dbo.Company`; never infer it from this crawler's IDs or ticker order. |
| FiscalYear | `Year` | Reported. |
| FiscalQuarter | `Period = 4` | Annual source code. Income amounts cover the full fiscal year, not October–December only. |
| PeriodEndDate | Fiscal year + December 31 | Inferred for supported calendar-year issuers; not present in the workbook. |
| PublishDate | `Publish Date` | Date in Europe/Istanbul, with full source timestamp retained in provenance. |
| Revenue | `Revenue`, else `Revenue from Finance Sector Operations` | Reported; presentation scale applied once. HOLDING-format issuers (KOÇ, SABANCI, ŞİŞECAM, TURKCELL, …) report industrial turnover here and finance-sector turnover separately: `field_status.Revenue = reported_excluding_finance_sector_revenue`, the finance line is in `source.unrounded_mapped_amounts.FinanceSectorRevenue`. GENERAL-format investment/brokerage holdings (ÜNLÜ) report only the finance line: `field_status.Revenue = finance_sector_revenue_reported_as_revenue`. |
| OperatingIncome | None | Missing from this workbook. |
| NetIncome | `Net Profit (Loss)` by default | Total group profit; `--net-income-basis owners-of-parent` explicitly selects the owners' profit field instead. |
| Ebitda | None | Missing; neither a verified reported EBITDA nor sufficient derivation inputs are present. |
| TotalAssets | `Total Assets` | Reported. |
| TotalLiabilities | Current + non-current liabilities | Derived, with the method recorded. Accounting identities are checked with the parser's source-scale tolerance. |
| Equity | `Total Equity` | Reported. |
| TotalDebtShort | None | Missing. Current liabilities include obligations beyond borrowings and are not substituted. |
| TotalDebtLong | None | Missing. Non-current liabilities are not substituted for borrowing debt. |
| CashAndEquivalents | None | Missing. |
| CurrentLiabilities | `Current Liabilities` | Reported. |
| NonCurrentLiabilities | `Non-current Liabilities` | Reported. |
| FreeCashFlow | None | Missing; operating cash flow and capital expenditure inputs are absent. |
| Eps | None | Missing; basic/diluted basis also needs a client definition. |
| SharesOutstanding | None | Missing; do not infer period-end shares from net income divided by EPS. |

Every missing source amount stays `null`. A real reported zero remains an exact
zero. JSON uses decimal strings because binary floating-point numbers can lose
precision when passed between applications. Preserve these as Decimal values when
loading into SQL Server; the money fields target `DECIMAL(22,4)`.

The numeric formatter flags overflow and any rounding instead of silently
truncating a source value. The supplied EPS and shares fields are missing, so no
value is synthesized for their `DECIMAL(14,4)` / `DECIMAL(22,2)` columns.

## Install this feature with replacement files

Extract `CompanyFundamental_Replacement_Files.zip` into your Ubuntu Downloads
folder. The archive contains `company-fundamental-replacement/files/` with the
same paths as your project. These files are based on the source archive reviewed
in this conversation. No patch command is needed.

From your existing project, save the current CLI file:

```bash
cd ~/projects/stock-crawler
cp -n src/stock_crawler/main.py src/stock_crawler/main.py.before-client-export
```

Copy the provided files into the project:

```bash
cp -a ~/Downloads/company-fundamental-replacement/files/. .
```

This replaces `src/stock_crawler/main.py` and adds/replaces
`src/stock_crawler/client_export.py`, `tests/test_client_export.py`, and this
`docs/history/CLIENT-FUNDAMENTALS.md` guide. If you changed these files since the reviewed
archive, merge those edits with the supplied files before copying. SQL data,
credentials and Compose configuration are not included in the archive.

Build the crawler image with the updated code:

```bash
docker compose build crawler
```

Stop if the copy or build fails. Build-time internet access is separate from the
crawler's runtime SOCKS connection. After the build succeeds, continue below.

## Run against your already-downloaded documents

```bash
cd ~/projects/stock-crawler
bash scripts/crawler-ubuntu.sh docker export-company-fundamentals \
  --currency TRY \
  --output /app/data/CompanyFundamental_Review.json
```

The host file is `data/CompanyFundamental_Review.json`. The default source search is
`data/raw/_exports/*/source.xlsx`; it does not trigger a crawl. The existing Ubuntu
launcher may start SQL Server as a Compose dependency, but this command does not
connect to SQL. The proxy can be stopped for the export itself.

For the attached data, expect four records and zero complete records. Missing
CompanyId is reported until you supply the client's mapping. The other eight
required fields remain missing even after the IDs are mapped.

**Exit code 1 is expected for this incomplete contract**, with the review JSON
still written. It can also mean a source/validation failure, so read the report.
Exit 0 means selected records passed required-field/numeric checks; it does not
verify foreign keys or resolve the semantic decisions below. No insert is executed.

To read one specific workbook instead, add `--input /app/data/raw/_exports/HASH/source.xlsx`
using the actual hash directory. You can also add `--tickers THYAO,ASELS`.

When the client supplies real ticker-to-CompanyId mappings, save the JSON object
in `config/client_company_ids.json`, with integer values from their database:

```bash
bash scripts/crawler-ubuntu.sh docker export-company-fundamentals \
  --currency TRY \
  --company-map /app/config/client_company_ids.json \
  --net-income-basis total-profit \
  --output /app/data/CompanyFundamental_Review.json
```

Mapping keys must be normalized ticker symbols and IDs must be positive SQL INTs.
Duplicate mapping keys and repeated IDs across tickers are rejected for review.
There are deliberately no fabricated example client IDs in the delivered results.

## Selection and provenance

- The default scope policy is `consolidated-else-unconsolidated`: consolidated
  when the issuer files one, otherwise the unconsolidated statement, because an
  issuer without subsidiaries files only solo statements (250 of the 611 issuers
  in the September 19 workbooks). Each record says which case it is in
  `source.scope_basis` (`preferred_scope` / `fallback_only_scope_available`) and
  `source.consolidation_scope`. `--scope consolidated` or `--scope unconsolidated`
  keep strictly one scope and skip the other, as before.
- For a company/year, later publication and then higher numeric notification ID
  wins among the supplied files. Byte-identical workbooks are deduplicated.
- Equal-rank sources with conflicting mapped amounts are flagged; no arbitrary
  numeric result is certified as complete.
- This offline selection does not read SQL withdrawal state and does not assert
  that the saved files are the latest available on KAP.
- `--currency` declares the expected target currency. Mismatches are flagged; the
  command does not perform currency conversion. The source currency and scaling
  remain in the JSON because the client's table has no currency column.
- Nonannual statements, bank/insurance/finance statement formats, unknown company
  identities, formulas, unknown workbook headers and invalid parsed records are
  rejected. The `errors` list is de-duplicated per notification and categorized in
  `error_counts_by_category` (`unsupported_statement_type`, `unmatched_company_title`,
  `missing_required_source_values`, `invalid_row`); rows repeated across overlapping
  workbooks are counted once.
- The parser's configured non-calendar-year exclusions are respected. Full
  reports are needed to verify exact period dates rather than infer them.

## What is needed for the complete client feature

Obtain an actual full financial statement package (not another ten-item comparison
workbook) for the same company, reporting period and consolidation scope, including
income statement, balance sheet, cash-flow statement and relevant debt/EPS/share
notes. Verified additional KAP export concepts could be another input route, but
they are not mapped or fetched by this implementation.

The client also needs to settle these definitions before production insertion:

1. **Quarter semantics:** annual/YTD or standalone quarter. Full-year revenue is
   not Q4-only revenue; Q4-only conversion needs compatible nine-month inputs and
   restatement handling. Balance-sheet snapshots are not differenced like flows.
2. **NetIncome:** total group profit or profit attributable to owners.
3. **Debt:** included borrowing components, current maturities and lease policy,
   with no double counting of subtotals and their components.
4. **EBITDA and FCF:** reported measure or an agreed formula with identified inputs.
   Do not calculate EBITDA from revenue and net income alone or substitute net
   cash change for FCF.
5. **EPS and shares:** basic/diluted EPS and period-end/weighted-average share count.
   IAS 33 uses a weighted-average denominator for EPS, which is different from a
   simple period-end share count. See [IAS 33](https://www.ifrs.org/issued-standards/list-of-standards/ias-33-earnings-per-share/).
6. **Period dates and CompanyId:** verified report dates and IDs in the client's
   actual company table.

Keep the current rich/versioned crawler storage. Use a separate nullable staging
contract while collecting missing fields, then promote only validated complete
records to the client's strict table. The target also needs a currency/scope
policy and a correction-history policy: its three-column primary key cannot
represent multiple scopes or versions for the same company/period.

Do not remove all NOT NULL constraints or fill missing values with zero merely
to make an insert succeed. Any agreed schema change should be explicit.

## Validation performed

- 23 new tests cover real workbook values/scaling, eight unavailable metrics,
  client IDs, income basis, currency mismatch, scope selection, duplicate/conflicting
  versions, unsupported sources, formula rejection and SQL precision limits.
- CLI validation proves the review command does not call HTTP or SQL and writes
  the diagnostic output with exit code 1 when the contract is incomplete.
- The full existing Python test suite plus the new tests passed; the opt-in live
  SQL integration test was skipped because no SQL Server was available here.
- The actual uploaded 2024–2025 workbook was processed: four records, no source
  errors, no numeric/accounting issues, no HTTP requests and eight missing required
  financial fields in every row. CompanyId is also unset without a client mapping.
- Docker rebuild and client database insertion were not exercised. This package
  does not claim to extract fields absent from the source document.
