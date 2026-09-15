# Stock Fundamental Crawler — KAP annual fundamentals → SQL Server → Grafana

A batch Python CLI that downloads KAP's **comparison export** (the same
`/en/api/export/compareItems` XLSX the v4/v5 browser scripts use), keeps every
source workbook, parses it locally, and publishes validated annual records to SQL
Server. Grafana reads SQL views; nothing downstream calls KAP.

Ten fields per company-year come from the export (revenue, net profit, owners'/NCI
profit, total assets, total equity, current/non-current liabilities, liabilities+equity,
finance-sector revenue). Values are stored in base currency units (already scaled).

## Quick start (Linux host with Docker)

```bash
git clone <this repo> ~/projects/stock-crawler && cd ~/projects/stock-crawler
make setup          # writes .env with unique passwords, builds the image, starts SQL Server, creates schema + logins
```

Then get data in, one of:

```bash
# A) You already have downloads from the v4/v5 browser scripts (kap_fundamentals_manifest*.json):
cp ~/Downloads/kap_fundamentals_manifest*.json imports/
make import         # zero HTTP; imports every year in every manifest

# B) Fetch live. Put tickers in config/companies.txt (one per line), set KAP_YEARS in .env, then:
make sync           # 25 companies per request, 5-10 s between requests
```

```bash
make grafana        # http://127.0.0.1:3000  (admin / GF_SECURITY_ADMIN_PASSWORD from .env)
```

That is the whole setup. `make` with no target lists everything; every `make` target is a
one-line `docker compose run --rm crawler <command>` you can also type yourself.

Before running `sync`, review `HTTP_USER_AGENT` in `.env` (operator contact) and KAP's
current access terms; the export endpoint is public but undocumented.

## Getting data

### A) Import existing browser downloads (no KAP access needed)

The v4/v5 scripts save a JSON manifest of base64 XLSX workbooks. Copy them into
`imports/` and import; nothing is sent to KAP:

```bash
docker compose run --rm crawler import-kap-export --input imports/kap_fundamentals_manifest.json imports/kap_fundamentals_manifest_2016_2020.json
```

- Add `--dry-run --output data/validation.json` first to see what would be published
  without opening SQL Server.
- Every annual row in the file is imported (all years). Re-importing the same file is a
  no-op; a byte-different workbook with identical values does not create new versions.
- Companies are matched by **exact title** against `config/kap_companies.json` (754
  ticker/ID/title entries recovered from the scripts). A renamed issuer shows up as
  `unmatched export company ...` — add its old title to that entry's `aliases` list.
- Manifests have no original download timestamp, so `retrieved_at` is the import time
  and the record says so in its provenance.

### B) Live sync

```bash
docker compose run --rm crawler sync                      # config/companies.txt, KAP_YEARS from .env
docker compose run --rm crawler sync --tickers THYAO,ASELS
docker compose run --rm crawler sync --company-file config/companies_candidates.txt   # all 754
```

- One POST covers 25 companies × up to 5 years (`KAP_YEARS`, a KAP limit). All requested
  years are published, not just the latest.
- A company is "fresh" for `DISCOVERY_INTERVAL_HOURS` (24) after a successful sync and is
  skipped; `--refresh` overrides that. Failed companies stay due.
- `MAX_COMPANIES_PER_RUN` (250 ≈ 10 requests) and `MAX_REQUESTS_PER_RUN` bound a run;
  companies over the cap are reported as `deferred` and picked up by the next run, so a
  daily cron entry walks through the full list:

```cron
0 6 * * * cd /path/to/stock-crawler && docker compose run --rm crawler sync >> data/cron.log 2>&1
```

- HTTP 429 records a cooldown (≥ 1 h or `Retry-After`); 401/403 or a challenge page
  stops the host until you clear `data/state/` deliberately. No retries evade these.

### C) Make a new browser export

If the server cannot reach KAP but your browser can:

```bash
docker compose run --rm crawler build-kap-script --company-file config/companies_candidates.txt --years 2021,2022,2023,2024,2025 --output config/export_2021_2025.js
```

Open <https://www.kap.org.tr/en/kalem-karsilastirma>, paste the script into the
developer console, and let it run (`kapProgressStatus()`, `kapStop()`, `kapResume()`,
`kapDownloadSoFar()` are available). It resumes across runs, honours 429 cooldowns, and
downloads a manifest you import with (A).

## "ConnectTimeout" from `probe-source` — KAP unreachable from the container

`probe-source` doing two GETs and reporting `transport failure ... ConnectTimeout` means DNS
resolved but TCP to kap.org.tr never answered from inside Docker. Find out where it breaks:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' --connect-timeout 10 https://www.kap.org.tr/en
```

```bash
docker run --rm curlimages/curl -sS -o /dev/null -w '%{http_code}\n' --connect-timeout 10 https://www.kap.org.tr/en
```

- **Host 200, container timeout** → Docker egress. Typical causes: the host reaches KAP only
  through a VPN/proxy that containers don't use (set `HTTPS_PROXY=socks5://host.docker.internal:PORT`
  in `.env`; the crawler service maps `host.docker.internal` to the host), a firewall
  dropping the `docker0` FORWARD chain (`sudo ufw status`, DOCKER-USER rules), or an MTU
  mismatch on VPN links (`"mtu": 1400` in `/etc/docker/daemon.json`).
- **Both time out** → the network itself can't reach KAP (KAP drops ICMP, so `ping` proves
  nothing; use the curl above). Use route (A) or (C) — they only need a browser that works.
- **Both 200** → rerun `make probe`. It exits 0 when the site root answers; KAP returns a
  non-standard status for `robots.txt`, which is reported but not treated as failure.

Related: `docker compose ps` shows only the services you started — Grafana appears after
`make grafana`. `systemctl status grafana-server` always says "not found" because Grafana runs
in Docker here, and containers from other projects in `docker ps` are unrelated.

## Configuration (`.env`)

`.env.example` is complete and commented; `scripts/setup-linux.sh` copies it with unique
passwords. Paths are relative to the checkout and work identically in Docker (`/app`).

| Key | Default | Meaning |
|---|---|---|
| `SOURCE_MODE` | `kap-export` | `kap-export` = live KAP export; `fixture` = replay `tests/fixtures/kap/source` offline (tests only, stored under `market_source='kap'`, hidden in Grafana) |
| `COMPANY_FILE` | `config/companies.txt` | active tickers, one per line, `#` comments |
| `KAP_COMPANY_REGISTRY` | `config/kap_companies.json` | ticker → KAP member id + exact title (+ optional `aliases`) |
| `KAP_YEARS` | `[2024,2025]` | 1–5 years per request |
| `KAP_NON_CALENDAR_YEAR_TICKERS` | `[]` | issuers whose financial year is not Jan–Dec; they are skipped (the export has no period dates, so Jan 1–Dec 31 is inferred for everyone else and flagged in the record's warnings) |
| `MAX_COMPANIES_PER_RUN` / `MAX_REQUESTS_PER_RUN` | `250` / `50` | per-run bounds |
| `REQUEST_DELAY_MIN/MAX_SECONDS` | `5` / `10` | pacing between requests |
| `DISCOVERY_INTERVAL_HOURS` | `24` | how long a successful sync counts as fresh |
| `HTTPS_PROXY` | unset | optional proxy for the crawler container only |
| `MSSQL_*`, `GF_*` | — | SQL Server / Grafana credentials; see the template |

`init-db` creates the database, schema, and the `crawler_writer` / `reader` /
`grafana_reader` logins. It does **not** rotate passwords of logins that already exist —
changing `.env` alone does not change SQL credentials.

## Operations

| Command | What it does |
|---|---|
| `sync` | fetch + publish due companies (exit `0` ok, `1` some company failed, `2` stopped by budget/cooldown/block, `3` another run holds the lock) |
| `import-kap-export --input f1 [f2 ...]` | import manifests/XLSX; `--dry-run` validates without SQL |
| `reprocess [--tickers ...]` | re-parse every stored workbook with the current parser, no HTTP |
| `compare --before-report-id A --after-report-id B` | field-level diff of two stored versions |
| `probe-source` | reachability check (two GETs) |
| `build-kap-script` | generate a browser exporter for a company/year selection |
| `companies-from-csv --input x.csv --output y.txt` | extract unique tickers from a CSV column |
| `scripts/backup.sh` / `scripts/restore.sh backups/<ts>` | SQL `.bak` + raw workbooks + state (restore is destructive) |

Run summaries land in `data/runs/<run>/summary.json`. Raw workbooks are kept once by
SHA-256 under `data/raw/_exports/`; per-record snapshots and parser output under
`data/raw/kap_compare/`. A changed value creates a new report version — old rows are never
overwritten. `sync`, backup and restore share one OS lock (`data/state/crawler.guard`).

## Reading the data

- `dbo.vw_fundamentals` — every valid version per company/year/scope.
- `dbo.vw_latest_fundamentals` — one latest annual row per company.
- Filter `market_source = 'kap_compare'` (Grafana's dashboard already does). Monetary values
  are already in base units — do **not** multiply by `currency_scale` again. Missing = `NULL`.
- The comparison page is delayed and shows current-period columns only: no prior-period
  restatements, no withdrawal detection, no exact period dates. Suitable for periodic
  fundamentals screens, not for point-in-time backtests. EPS, operating income, cash, debt,
  EBITDA, FCF and shares are `NULL` for this ten-item product.

## Native development (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                    # 140 offline tests
node --test tests/browser_core.test.cjs   # optional, Node 18+
cp .env.example .env                      # relative paths; SQL is not needed for the commands below
stock-crawler import-kap-export --input tests/fixtures/kap/exports/two_companies_2023_2024.xlsx --tickers THYAO,ASELS --dry-run
```

Expected: 4 rows, 0 import errors, 4 valid annual records. The opt-in SQL integration test
needs `STOCK_CRAWLER_TEST_MSSQL_URL` pointing at a **disposable** database.

See [REVIEW.md](REVIEW.md) for the 2026-09-14 review findings and the data-quality
decisions behind the design.

## Layout

```text
src/stock_crawler/kap_export.py     KAP export client (batched POST), XLSX parsing, registry, manifest import
src/stock_crawler/kap.py            source interface, fixture client, annual-selection rule, probe
src/stock_crawler/fetch.py          paced/budgeted HTTP with cooldown + block handling
src/stock_crawler/pipeline.py       sync, import, reprocess orchestration
src/stock_crawler/parser.py         HTML fixture parser + export dispatch
src/stock_crawler/storage.py        raw store, run summaries, state, OS lock
src/stock_crawler/db.py             SQL Server repository + init-db
src/stock_crawler/browser/          generated browser exporter (v6)
sql/schema.sql                      tables and views
config/kap_companies.json           754 ticker/ID/title identities from the supplied scripts
config/companies.txt                active list (start small); companies_candidates.txt = all 754
```
