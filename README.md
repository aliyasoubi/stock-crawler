# Stock Fundamental Crawler

Batch Python CLI that captures the latest annual financial statement notification per Turkish
company from KAP, stores immutable raw snapshots, extracts validated fundamentals, and writes
them to Microsoft SQL Server. Consumers and Grafana read SQL views only.

This repository implements the specification in `README_Stock_Fundamental_Crawler_MVP_v8`
(kept outside the repo). Section numbers below refer to that document.

## Status

| Area | State |
| --- | --- |
| Config, company list, CSV candidate utility (§4, §13) | Implemented, tested |
| Filing selection rule (§5) | Implemented as pure logic, tested |
| Paced/budgeted HTTP client, cooldowns, access stops (§6) | Implemented, tested with a mock transport |
| Raw snapshots, manifests, state, run lock, run summaries (§7) | Implemented, tested |
| Parser: KAP-style HTML → canonical fields, units, derived metrics (§9, §10) | Implemented against a **synthetic reference fixture**; label dictionary must be reviewed against real captured filings |
| SQL schema, views, least-privilege roles, `init-db` (§8, §14) | Implemented; verified against SQL Server 2022 in Docker (idempotent re-run, version ordering, withdrawal exclusion, reader cannot write) |
| Sync / reprocess / compare orchestration (§11, §13) | Implemented, tested end-to-end offline with a fixture source and in-memory repository |
| Docker Compose, Grafana provisioning + dashboard (§14) | Verified: clean startup, datasource health OK via `grafana_reader`, dashboard provisioned and querying the views |
| Backup / restore (§15) | `scripts/backup.sh` + `scripts/restore.sh`; one full destroy-and-restore cycle verified |
| Live smoke check and fixture capture (§6, §15) | `probe-source` (2 paced GETs) and `capture-fixture` (one operator-supplied URL) implemented, tested with a mock transport |
| **Live KAP retrieval** (§6) | **Pending an access route.** `KapClient` discovery/download methods raise `SourceAccessNotConfigured`. `SOURCE_MODE=fixture` runs the full pipeline offline; `probe-source` and `capture-fixture` are the first two live steps. |

## Layout

```
src/stock_crawler/
  main.py       CLI (init-db, sync, reprocess, compare, companies-from-csv)
  pipeline.py   sync/reprocess orchestration (injected repository + source client)
  config.py     validated settings, company-file loading
  models.py     typed records (CompanyIdentity, FilingCandidate, FundamentalRecord, ...)
  kap.py        SourceClient protocol, selection rule, FixtureSourceClient, KapClient stub
  fetch.py      PacedClient: pacing, global budget, retries, Retry-After, cooldowns, access stops
  storage.py    RawStore (atomic snapshots), StateStore, RunLock, RunSummary
  parser.py     HTML → StatementFacts (IR) → ParsedReport; concept dictionary; validation
  metrics.py    pure derived-metric functions with explicit methods/reasons
  units.py      Turkish numerals, presentation currency/scale, dates, Istanbul→UTC
  compare.py    normalized report/row comparison and classification
  csvtools.py   candidate tickers from the shared CSV (no network)
  db.py         SQLAlchemy/pyodbc repository, readiness check, init-db bootstrap
sql/schema.sql  idempotent tables, views, roles, schema version
config/companies.txt
tests/          110 offline tests + 2 SQL Server integration tests; fixtures under tests/fixtures/kap/source
scripts/        setup-linux.sh, backup.sh, restore.sh
grafana/        datasource + dashboard provisioning
```

Deviations from the spec's file table, all deliberate: the HTTP pacing layer lives in
`fetch.py` (source-agnostic, separately testable) rather than inside `kap.py`; orchestration
lives in `pipeline.py` so it can be tested with fakes; `compare.py`, `csvtools.py`, and
`units.py` are small pure modules split out of `db.py`/`parser.py`.

## Running locally (offline, no SQL Server)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

## Running with Docker (Ubuntu)

Three containers: `mssql` (SQL Server 2022, data in a named volume), `crawler` (a one-shot CLI
image invoked with `docker compose run`, never left running), and `grafana`. Requirements:
Docker Engine with the compose plugin, x86-64 host (the SQL Server image is amd64-only; on an
ARM host it runs under emulation), and at least 2 GB RAM free for SQL Server.

```bash
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin   # per docs.docker.com/engine/install/ubuntu
sudo usermod -aG docker "$USER" && newgrp docker

git clone <this repo> && cd trading
scripts/setup-linux.sh          # creates .env, sets CRAWLER_UID/GID to your user, creates data dirs
nano .env                       # replace every REPLACE_WITH_* value (sa password must satisfy SQL Server policy)

docker compose build crawler
docker compose up -d --wait mssql
docker compose run --rm crawler init-db
docker compose run --rm crawler sync --tickers THYAO
docker compose run --rm crawler sync
docker compose up -d grafana     # http://127.0.0.1:3000 (admin / GF_SECURITY_ADMIN_PASSWORD)
```

`CRAWLER_UID`/`CRAWLER_GID` matter on Linux: bind mounts keep host ownership, so the crawler
container runs as your user to write `./data` and `./config`. Ports are published on
`127.0.0.1` only; use an SSH tunnel or a reverse proxy for remote access, never expose `sa`.

Recurring refresh: a host cron entry such as
`0 6 * * * cd /opt/trading && docker compose run --rm crawler sync >> data/cron.log 2>&1`
is enough; the run lock rejects overlaps and cooldowns persist under `data/state`.

`SOURCE_MODE=fixture` (the default in `.env.example`) replays `tests/fixtures/kap/source`
with zero network requests, which is enough to prove the database, views, Grafana, reruns,
reprocessing, and comparisons. Switch to `SOURCE_MODE=kap` only after wiring `KapClient`.

Other commands:

```bash
docker compose run --rm crawler sync --company-file /app/config/companies.txt --limit 25 --refresh
docker compose run --rm crawler reprocess --tickers THYAO
docker compose run --rm crawler compare --before-report-id 1 --after-report-id 2
docker compose run --rm crawler companies-from-csv --input /app/imports/kap_fundamentals.csv --ticker-column stock_code --output /app/config/companies_candidates.txt
```

Live-source commands (explicitly invoked, tiny request counts, all pacing/stop rules apply):

```bash
docker compose run --rm crawler probe-source                       # robots.txt + site root, 2 GETs
docker compose run --rm crawler capture-fixture --url <exact notification URL from your browser> \
    --ticker THYAO --source-company-id <KAP company id> --notification-id <id> \
    --published-at "05.03.2025 18:45:00" --fiscal-year 2024 --period-end 2024-12-31 --scope consolidated
```

`capture-fixture` saves the response under `data/captures/` in the fixture layout; point
`FIXTURE_SOURCE_DIR` at it and run `sync --tickers THYAO` to parse it offline. That is how the
real KAP layout gets reviewed before any discovery code is written.

Exit codes: `0` success, `1` configuration/database/source error, `2` run stopped early or probe
not fully OK (budget, throttling, or access block; pending tickers are listed), `3` another run
holds the lock.

Backup and restore: `scripts/backup.sh` writes `backups/<timestamp>/` (native `.bak` plus a
tarball of `data/raw` and `data/state` with checksums); `scripts/restore.sh backups/<timestamp>`
replaces the database and raw directories and re-runs `init-db`. Verify with `reprocess`
(expect `already_parsed`).

Apple Silicon (development only): the SQL Server image is amd64-only; enable Rosetta emulation in Docker Desktop.

## Wiring live KAP access (the remaining Phase-1 step)

1. Decide the route (§6): KAP's REST service (Borsa İstanbul agreement, MKK authorization,
   registered IP, API key) or a permitted public-web pilot after checking the terms and the
   live `robots.txt` for the exact host and paths. From the authoring machine `kap.org.tr`
   did not answer at the TCP level, so expect network-level restrictions to be part of this step.
2. Capture one THYAO annual notification response in its native format and save it under
   `tests/fixtures/kap/` (small, permitted, with a `filings.json` entry).
3. Implement the three `KapClient` methods in `kap.py` (the docstring lists what each must
   populate) using `self.fetcher.get(...)`; pass stored validators for conditional requests.
4. If the payload is JSON/XML rather than HTML, add an extractor that produces the same
   `StatementFacts` IR; `build_report` and everything after it stay unchanged.
5. Review `parser.CONCEPTS` labels against the captured filing; extend `tests/test_parser.py`
   with the real fixture's expected values.

## Consumer contract

Read `dbo.vw_fundamentals` (one latest valid version per company/year/scope, current periods
only) or `dbo.vw_latest_fundamentals` (one row per company). Monetary columns are normalized
to base `currency_code` units — never multiply by `currency_scale` again. `eps` is currency per
share; `shares_outstanding` is a count. `NULL` means unavailable or not safely derivable, never
zero. `*_method` columns say how derived values were obtained (`direct`, `sum_borrowings`,
`sum_borrowings_and_leases`, `operating_income_plus_da`, `operating_cf_minus_capex`,
`capital_less_treasury`, `missing`). Freshness: `published_at`, `retrieved_at`, `parsed_at`,
`last_discovery_at`, `last_success_at`, `last_error`, `latest_discovered_notification_id`.

## Backup and restore

`scripts/backup.sh` and `scripts/restore.sh` implement this; the notes below explain what they do.

- Raw evidence: `./data/raw` (immutable snapshot directories) and `./data/state` are archived
  together with the SQL backup; `data/runs` and `backups/` themselves are disposable/offsite.
- SQL: native `BACKUP DATABASE ... WITH CHECKSUM` inside the `mssql` container, copied out with
  `docker compose cp`. Restore uses `RESTORE DATABASE ... WITH REPLACE`, then `init-db` (idempotent)
  recreates logins on a new server. Keep backups off the Docker host.
- Restore check (verified once locally): `reprocess --tickers THYAO` reports `already_parsed`
  and `vw_latest_fundamentals` returns the pre-backup rows.

## Acceptance checklist (§15) — current standing

- [x] Clean Docker startup initialises SQL Server and runs the CLI (verified; `init-db` idempotent)
- [ ] Five companies resolve and yield selected annual filings — *blocked on live access; two synthetic fixtures prove the path*
- [x] Every published result has durable raw provenance and a parser version
- [ ] Required concepts and units checked against the exact source notification — *needs a real captured filing*
- [x] Canonical names, normalized amounts, scope, NULL, freshness documented
- [x] Repeating a run does not duplicate snapshots or redownload fresh data (tested)
- [x] Simulated correction and parser change retain earlier results with classified differences (tested)
- [x] Failed/unsupported records remain visible; no fabricated rows (tested)
- [x] Adding a company is list configuration only for a supported layout
- [x] Request limits and stop behaviour work under mocked failures (tested)
- [x] Application and Grafana read stored values without KAP connectivity (fixture mode, zero HTTP attempts)
- [x] SQL and raw-file backup/restore scripts with one local restore check
