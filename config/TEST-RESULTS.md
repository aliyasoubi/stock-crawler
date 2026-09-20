# Validation — 2026-09-19

## Automated checks

| Check | Actual result |
|---|---|
| Uploaded Python baseline, Python 3.12 | 163 passed, 2 failed, 1 skipped |
| Corrected Python suite, Python 3.12 | 178 passed, 1 skipped |
| JavaScript helper tests | 3 passed |
| Shell syntax | All 5 shell scripts passed `bash -n` |
| Build launcher routing | Stub Docker invocation verified that `build` forwards to the crawler service with the existing Compose files; no real image build |
| Python wheel | Built `stock_crawler-0.2.1-py3-none-any.whl`; schema/browser assets present, logger and CompanyId present, CLI prints 0.2.1 |
| Schema copies | Package and top-level schema files are byte-identical |
| Summary helper | Read the uploaded completed run and displayed its exact start/end timestamps and status counts |
| SQL Server integration | Skipped: no disposable SQL Server test URL |
| Docker / Grafana runtime | Not run: Docker unavailable here |
| Live full-list KAP crawl | Not performed |

The baseline failures were:

- `test_real_workbook_mapping_and_missing_fields`: `KeyError: CompanyId`.
- `test_client_ids_are_not_invented_and_income_basis_is_explicit`: same error.

The added tests exercise verified aliases and batch boundaries, unknown names,
conflicting notification rows, rejected-row exit codes, missing requested years,
client reuse/refresh, year-by-year freshness expiry, legacy coverage, parser-version
changes, crash summaries, configured caps, and measuring-unit comparisons. Existing
parser tests also verify current and comparative measuring-unit dates.

Reproduce the automated suite in a local development environment:

```bash
python3.12 -m venv .venv-test
.venv-test/bin/python -m pip install -e '.[dev]'
.venv-test/bin/python -m pytest -o addopts='' -q
node --test tests/browser_core.test.cjs
```

The SQL integration test remains opt-in and must use a disposable database.

## Replay of the uploaded workbook that triggered ARSAN

Used the exact uploaded native XLSX with SHA-256
`d93478146752220d78ba31354811cd087a86e65011e1b3b731217e4e8e84c689`.
Replayed its 47 source rows through HTTPX MockTransport and the actual sync/parser
pipeline using the in-memory test repository, selecting the 24 resolved issuers
and requested years 2024/2025. This was **zero live KAP requests and zero SQL Server
connections**. The source bytes were not edited.

| Pipeline result entries | Count |
|---|---:|
| Published | 36 |
| Unsupported | 6 |
| Failed validation | 1 |
| No eligible filing for a requested year | 5 |
| Rejected identity/metadata rows | 0 |

There are 48 result entries because 24 companies × 2 requested years are considered.
This is not a claim that 48 companies were downloaded or that every source row is
publishable; selection also chooses among consolidation scopes.

**ARSAN 2024 (notification 1404479) and 2025 (1571923) both published.**
The historic textile title and current holding title resolve to one source identity.

The failed validation is **ARENA 2024**: the workbook has no revenue or net-profit
values. Those values remain missing; the parser does not substitute zero.
Unsupported entries belong to **ANHYT, ANSGR, ARSNF and ARSVY** and require financial
sector mappings. The archived-workbook replay therefore remains a partial run even
though the ARSAN crash is fixed.

## What these checks do not establish

They do not validate a live SQL migration, production connectivity, Grafana rendering,
all 754 registry entries, unreviewed financial metrics, future changes at KAP, or
independent accounting correctness. The supplied ten-item comparison source still
has its original coverage limitations.
