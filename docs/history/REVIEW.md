# Project review and implementation result

> **Update 2026-09-15 — setup simplification.** After the review below, the operator surface
> was reduced: one `.env` with relative paths for Docker and native runs; `SOURCE_MODE=kap-export`
> is the default and the never-implemented `kap` stub was removed; the opt-in
> `KAP_CALENDAR_YEAR_TICKERS` became the opt-out `KAP_NON_CALENDAR_YEAR_TICKERS`; `sync` now
> sends 25 companies per export POST (like the original scripts) and publishes every requested
> year instead of only the latest; `import-kap-export` accepts several files; `make setup /
> import / sync / grafana` cover the whole flow. Verified with the offline suite (140 passed) and
> one live batched POST (THYAO + ASELS, 2024-2025: four valid records from one request).
> Names below reflect the state on 2026-09-14.

Reviewed: 2026-09-14. Input: `stock-crawler-main(1).zip` plus the supplied v4 and
v5 browser scripts. Output: application 0.2.0, parser 1.1.0.

## Assessment

The existing project is a useful foundation, but the uploaded version was not a
working live crawler. The source-client interface, immutable raw store, explicit
units, injected pipeline dependencies and SQL views are sensible for this MVP.
Most of the complexity serves real data-quality/recovery requirements. Keep the
batch design and avoid adding services until the source coverage is proven.

The critical gap was not another model or database layer: live source discovery
was unimplemented and the parser had only synthetic HTML fixtures. The colleague's
scripts expose a usable **different data product**: KAP comparison XLSX exports.
This review implements that route end to end, while preserving its limitations.

The package is ready for a **controlled MVP pilot**. It is not yet a fully verified
production release, an instant disclosure service, or a complete historical
financial-report archive. SQL Server/Grafana/restore and a full browser runner test
still need execution on the deployment machine. These are concrete release checks,
not claims of completed validation.

## Findings and changes

| Priority | Finding in the input | Change / current outcome |
|---|---|---|
| Critical | `KapClient` only raised `SourceAccessNotConfigured`. | Added `KapExportClient`, exact read-only export POST, real XLSX parsing, live annual selection and local replay. Reserved authoritative notification client remains explicitly unwired. |
| High | Scripts downloaded binary exports; there was no import path. | Added `import-kap-export` for legacy manifests, v6 manifests and standalone XLSX. It validates first, supports a no-SQL dry run, and ingests all historical annual rows. |
| High | Synthetic HTML alone did not establish correct live labels/units. | Captured a real four-row workbook, preserved the exact request, and added assertions for source values, Turkish separators, negative NCI and mixed presentation scales. |
| High | `.env` and concrete passwords were bundled; setup referenced a missing `.env.example`; ignore rules were named `gitignore.txt`. | Removed bundled credentials, restored standard ignore/template files, added Docker context exclusions, generated unique initial passwords and restrictive `.env` permissions. Rotate the old values if they were ever used outside disposable local testing. |
| High | A timestamp-based stale lock could replace an active run's lock after six hours, or across containers. | Replaced it with a stable OS guard lock. Process death releases it. Linux backup/restore share the same guard and preserve its inode. |
| High | A cron invocation could return success despite per-company failures; failed discovery could count as fresh. | Nonzero partial-failure exit status; failed issuers remain due; retries can re-fetch failed snapshots. |
| High | Reprocessing a withdrawn notification under a new parser could publish an unwithdrawn version. | Checks withdrawal status across stored versions before publishing; regression test covers parser changes. |
| High | Comparison exports and authoritative/synthetic notification records would otherwise appear interchangeable. | Uses `market_source='kap_compare'`; Grafana filters this product and one selected currency. Demo data stays under `kap`. |
| Medium | v4/v5 used positional batch indices and mutable fixed storage keys. | v6 identifies a job by configuration and a batch by its request hash; changing companies, years, IDs or items gets a separate job. |
| Medium | A 200 HTML/challenge response larger than 100 bytes could be marked complete. | Browser checks workbook signature/type; Python verifies XLSX structure, expected headers, formula/error cells and size limits before parsing. |
| Medium | LocalStorage could fill; save errors were logged while downloading continued. | v6 stores batch payloads in IndexedDB and waits for transaction completion. Save failures stop the run and retain an emergency download. |
| Medium | Script had no request budget, overlap prevention or persisted Retry-After. | v6 has a bounded run, a Web Lock, 5–10 second pacing, immediate 429 stop and persisted cooldown. Python POST uses the same paced/budgeted transport as GET. |
| Medium | Full financial restatement history cannot be obtained from the comparison page. | Documented in README and each export report's validation warnings. No fabricated backtest availability or withdrawal inference. |
| Medium | Binary XLSX packaging changes could look like financial changes; repeated imports could bloat storage. | Archive native workbook once by hash; version financial records by stable extracted row content. Repacked XLSX with identical values reuses report versions. |
| Medium | Cash-flow D&A was assumed to be wholly deducted from operating income. | Parser no longer derives EBITDA from that unverified assumption; leaves it NULL unless a reviewed direct value exists. Parser version increased. |
| Medium | A direct debt total plus partially known lease maturities could understate debt. | Reject incomplete lease buckets instead of summing partial values. |
| Medium | Listing period and parsed statement dates could disagree unnoticed. | HTML snapshot parser checks fiscal year and period end against discovery metadata. |
| Medium | Currency metadata changes could disappear in a comparison. | Reports currency/scale/date changes; scope differences prevent numeric deltas. |
| Medium | Reprocess visited only the latest company snapshot. | Reprocess visits every stored snapshot for that source and verifies native workbook integrity. |
| Medium | Corrupt cooldown JSON silently reset state. | Fails closed with an actionable error. |
| Medium | ODBC password delimiters and unsanitized database errors could expose/change connection configuration. | Escapes/braces connection values, hides bound parameters, and makes top-level SQL errors concise. |
| Medium | Installed wheels did not contain the SQL schema. | Schema and browser assets are package data; wheel contents checked. |
| Medium | Compose selected Developer edition while documentation discussed production. | Defaults to Express for the small MVP; documents edition choice and removes incompatible compressed-backup assumption. |

## What the colleague's scripts actually provide

The two scripts are the same implementation with different year windows,
progress keys and download filenames:

- v4 requests 2021–2025.
- v5 requests 2016–2020.
- Both have the same **754 unique company IDs/tickers** and request ten item IDs.
- Both request annual `periodList: ["4"]` and sector `GENERAL`, 25 companies at a time.
- Both return a JSON manifest of Base64 XLSX payloads. The value extraction happens
  later; it was absent from the uploaded project.

The company registry and candidate list were recovered exactly from these inputs.
A historical company title may differ from its current title. The importer requires
an exact verified title or an explicit alias; it never assumes row order proves identity.
The registry is static and needs maintenance for new listings, renamings and removals.

## Actual verification performed

| Check | Result |
|---|---|
| Original offline Python suite, before edits | 110 passed, 1 skipped |
| Updated Python suite | 137 passed, 1 skipped |
| JavaScript pure helper tests | 3 passed |
| Generated browser script | `node --check` passed; full browser lifecycle still untested |
| Real endpoint, minimal THYAO 2024 one-item request | HTTP 200; XLSX returned |
| Real endpoint, THYAO/ASELS 2023–2024 ten-item request | HTTP 200; 5,100-byte workbook, four rows, retained as a fixture |
| Real sample CLI dry run | Four annual records valid; zero import errors; zero SQL/HTTP calls during import |
| New Python client live smoke | One POST, two candidate periods, selected THYAO 2025 notification `1565996`, valid parse persisted into the in-memory test repository |
| Repeated import and XLSX repacking | Stable financial versions; native bytes retained separately |
| Native workbook corruption / invalid responses | Rejected; no new financial publication |
| Withdrawal + parser change | Remains excluded |
| Long-running and crashed processes | Active lock retained; dead-process lock released |
| Wheel packaging | SQL schema and browser assets included |
| Shell syntax / Python compilation | Passed |
| SQL Server integration test | Skipped: no SQL Server test URL; Docker is unavailable here |
| Docker/Grafana/backup/restore runtime | Not run in this review; original README's historical claims were not treated as current verification |
| robots.txt | Unavailable from this environment; reviewed page and limited exports worked |

Real-source examples after scaling, all in TRY:

| Issuer/year | Raw revenue | Scale | Normalized revenue |
|---|---:|---:|---:|
| THYAO 2024 | 745.430 | 1,000,000 | 745,430,000,000 |
| ASELS 2024 | 120.205.594 | 1,000 | 120,205,594,000 |

These examples verify extraction arithmetic; they are not an independent audit of
KAP's underlying financial statements. All monetary fields are already normalized
in SQL, so consumers must not apply the scale a second time.

## What remains before production or a broader scope

1. Execute the deployment checklist in README on the actual SQL Server/Docker host.
   Existing SQL passwords are not automatically changed by `init-db`; migrate/rotate
   credentials intentionally. Do not run integration tests against production.
2. Run the v6 browser exporter once in a real supported browser, save its manifest,
   and import it. Only helper behavior and generated syntax were tested here.
3. Confirm calendar-year reporting and company aliases for each added issuer.
   The XLSX does not contain exact start/end dates; unconfirmed issuers are not published.
4. Check current source access rules before scaling the working pilot. A successful
   export does not promise unlimited request capacity or a stable supported API.
5. If scope includes immediate notifications, withdrawal detection, prior-period
   restatements, non-calendar issuers or the seven extra metrics, add reviewed
   notification/financial-report fixtures and mappings. The ten-item comparison
   endpoint alone cannot supply those guarantees.

Do not add a queue or browser automation service to address these gaps. The next
useful work is source coverage and deployment validation, using preserved exports.

## Sources

- [KAP comparison page and its data limitations](https://www.kap.org.tr/en/kalem-karsilastirma)
- The exact XLSX response and request under `tests/fixtures/kap/exports/`.
- [openpyxl read-only workbook processing](https://openpyxl.readthedocs.io/en/stable/optimized.html)
- [Microsoft SQL Server Docker setup and platform requirements](https://learn.microsoft.com/en-us/sql/linux/install-upgrade/quickstart-install-docker?view=sql-server-ver17)
- The three user-provided attachments; all code findings above are grounded in their contents.
