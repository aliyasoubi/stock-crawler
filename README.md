# Stock Fundamental Crawler

Collect KAP annual financial statement comparison exports, retain the original
workbooks, publish validated records to SQL Server, and explore them in Grafana.
Grafana reads the database; opening or refreshing a dashboard does not contact KAP.

This guide matches the supplied project version: Python package `0.2.1`, SQL Server
2022 CU16, Grafana OSS 11.3.0, and the included Ubuntu proxy overrides.

## Choose your starting point

- **September 19 crash / upgrading an existing installation:** follow [FIXES-2026-09-19.md](FIXES-2026-09-19.md). Rebuild the crawler image before applying the schema migration or syncing.
- **Already installed and working:** use [Everyday crawling](#everyday-crawling).
- **New installation:** follow [First installation on Ubuntu](#first-installation-on-ubuntu).
- **Expand beyond THYAO and ASELS:** follow [Collect the candidate company list](#collect-the-candidate-company-list).
- **Check stored results:** use [SQL and Grafana](#sql-and-grafana).

Run commands from the same project directory, normally `~/projects/stock-crawler`.
Execute each block separately and stop if it fails. Terminal commands below do not
include shell prompts such as `$`, `arash@...`, or SQL prompts such as `1>`.

## First installation on Ubuntu

### 1. Prepare the project and prerequisites

Install Docker Engine with the Compose plugin using the
[official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/).
This workflow targets ordinary local Docker Engine on Ubuntu x86-64. The SQL image
is AMD64-only; remote Docker engines and rootless networking require separate checks.
The host needs Python 3, Bash, and a working `docker` command for your user.

Extract the project into `~/projects/stock-crawler`. Confirm the included files:

```bash
cd ~/projects/stock-crawler
docker version
docker compose version
python3 --version
ls docker-compose.yml compose.db-local.yml compose.proxy-host.yml scripts/crawler-ubuntu.sh
```

Keep the same directory and Compose project name when using an existing database;
a different project name normally selects different named volumes.

### 2. Create the environment without replacing existing passwords

`scripts/setup-linux.sh` reads the checked-in `.env.example` template:

```bash
cd ~/projects/stock-crawler
bash scripts/setup-linux.sh
```

On a new installation, the script creates `.env` with generated passwords,
sets the host UID/GID, and creates the data directories. If `.env` exists, it
preserves its credentials. Do not use the example passwords for a new installation.
Do not load `.env` with `source`; it is configuration data, not a shell script.

Only `.env` is loaded. Keep it out of Git; the ignore rules cover it.

### 3. Apply the live collection settings

The supplied template starts in **fixture mode**, with a **25-company run cap**.
The Python code has different defaults, but values present in `.env` override them.
Use this block once on a new installation, or on an existing installation when
adopting the settings below. It backs up `.env` and preserves all password values:

```bash
python3 - <<'PY'
from pathlib import Path
from datetime import datetime
import re
import shutil

path = Path('.env')
backup = path.with_name('.env.before-crawl-settings-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
shutil.copy2(path, backup)
text = path.read_text()
settings = {
    'SOURCE_MODE': 'kap-export',
    'COMPANY_FILE': '/app/config/companies.txt',
    'KAP_COMPANY_REGISTRY': '/app/config/kap_companies.json',
    'DATA_DIR': '/app/data',
    'KAP_YEARS': '[2024,2025]',
    'REQUEST_DELAY_MIN_SECONDS': '15',
    'REQUEST_DELAY_MAX_SECONDS': '30',
    'REQUEST_TIMEOUT_SECONDS': '60',
    'MAX_RETRIES': '2',
    'DISCOVERY_INTERVAL_HOURS': '24',
    'MAX_COMPANIES_PER_RUN': '1000',
    'MAX_REQUESTS_PER_RUN': '40',
    'ALL_PROXY': 'socks5h://127.0.0.1:10808',
}
for key, value in settings.items():
    pattern = rf'^{re.escape(key)}=.*$'
    line = f'{key}={value}'
    if re.search(pattern, text, flags=re.M):
        text = re.sub(pattern, lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip('\n') + '\n' + line + '\n'
path.write_text(text)
print('Updated collection settings; existing passwords preserved.')
print('Backup:', backup.name)
PY
```

The settings deliberately keep the demonstrated annual years, **2024 and 2025**.
Change `KAP_YEARS` when you want other periods; the implementation accepts 1–5
unique years. A current calendar year may not yet have a full annual statement.
Review `HTTP_USER_AGENT` in `.env` and replace the example operator contact with
your actual contact. Retain accurate `KAP_NON_CALENDAR_YEAR_TICKERS` exclusions;
the old `KAP_CALENDAR_YEAR_TICKERS` name is not used by this code.

The delay is an operational starting point, not a published KAP request allowance.
No pacing configuration guarantees access or complete data coverage.

### 4. Obtain the images and initialize SQL Server

```bash
docker compose pull mssql grafana
docker compose build crawler
```

**Build/download networking is separate from crawler networking.** The SOCKS
settings below configure the running crawler; they do not automatically configure
Docker image pulls, build-time package downloads, or Ubuntu package downloads.
Resolve a failed pull/build before continuing. See also [Reuse working images](#reuse-working-images).

```bash
bash scripts/crawler-ubuntu.sh db
bash scripts/crawler-ubuntu.sh docker init-db
```

The `db` command publishes SQL Server on `127.0.0.1:1433` for the host-networked
crawler. `init-db` creates the schema and the `crawler_writer`,
`fundamentals_reader`, and `grafana_reader` accounts. It does not rotate passwords
for existing SQL logins; editing `.env` alone will not change those passwords.

### 5. Start Grafana

The attached `docker-compose.yml` already includes
`GODEBUG: "x509negativeserial=1"` for the observed local SQL certificate issue.
No separately downloaded TLS-fix script is required for this version.

```bash
docker compose \
  -f docker-compose.yml \
  -f compose.db-local.yml \
  -f compose.proxy-host.yml \
  up -d --no-deps --force-recreate --pull never grafana
```

After startup, check:

```bash
docker compose ps
curl --noproxy '*' --max-time 5 http://127.0.0.1:3000/api/health
```

Open [Grafana](http://127.0.0.1:3000) on the Ubuntu computer. The username is
`GF_SECURITY_ADMIN_USER` (normally `admin`); use `GF_SECURITY_ADMIN_PASSWORD` from
`.env`. Grafana's existing volume can retain a previously configured password.
A healthy web endpoint does not prove the SQL datasource can connect.

### 6. Start the SSH SOCKS tunnel and perform a pilot crawl

Keep your usual SSH/AutoSSH tunnel running on `127.0.0.1:10808`. If it is not running,
this interactive command asks for your actual SSH destination and port. Run it in
a separate terminal and leave that terminal open:

```bash
read -r -p 'SSH destination (user@hostname or user@IP): ' crawler_ssh_destination
read -r -p 'SSH port [22]: ' crawler_ssh_port
ssh -N -D 127.0.0.1:10808 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -p "${crawler_ssh_port:-22}" "$crawler_ssh_destination"
```

Use your existing tunnel if one is already listening; do not start another on the
same port. Back in the project terminal:

```bash
ss -ltnp 'sport = :10808'
curl --proxy socks5h://127.0.0.1:10808 --noproxy '' \
  --connect-timeout 10 --max-time 30 \
  -sS -o /dev/null -w 'Host HTTP %{http_code}\n' \
  https://www.kap.org.tr/en
bash scripts/crawler-ubuntu.sh docker probe-source
bash scripts/crawler-ubuntu.sh docker sync --tickers THYAO,ASELS
```

`probe-source` checks the site root and `robots.txt`, including any redirects; it
does not test the export POST. A `robots.txt` HTTP 666 was observed in this setup.
That is a nonstandard response, not proof that export access works or is permitted.
A successful pilot POST, parsing result, and SQL query provide the end-to-end check.
Do not repeat the probe before every normal crawl.

## Everyday crawling

After installation, you do not need to rebuild images, initialize the database,
or reimport the dashboard for each run. Keep the SOCKS tunnel available and run:

```bash
cd ~/projects/stock-crawler
bash scripts/crawler-ubuntu.sh db
bash scripts/crawler-ubuntu.sh docker sync
```

This uses `config/companies.txt`, which contains THYAO and ASELS in this archive.
To collect the complete candidate file, use the command in the next section.

After a reboot, start Grafana if necessary:

```bash
docker compose -f docker-compose.yml -f compose.db-local.yml -f compose.proxy-host.yml up -d --no-deps grafana
```

Both SQL Server and Grafana have `restart: unless-stopped`. The crawler is a
one-shot CLI and exits after its run. Grafana can display stored results while
the crawler and SSH tunnel are stopped.

## Collect the candidate company list

`config/companies_candidates.txt` and `config/kap_companies.json` contain a dated
seed of 754 ticker identities from the supplied export scripts. They are not a
live list of every currently listed company. Some issuers may be renamed,
unsupported, or missing a requested annual report.

After the two-company pilot succeeds, try up to 100 due companies:

```bash
bash scripts/crawler-ubuntu.sh docker sync \
  --company-file /app/config/companies_candidates.txt \
  --limit 100
```

Review the summary. If there is no block or throttle, use this as the regular
full-list command:

```bash
bash scripts/crawler-ubuntu.sh docker sync \
  --company-file /app/config/companies_candidates.txt
```

The implementation batches 25 companies into one POST, including all selected
years. If all 754 companies are due and supported, this is about 31 initial export
requests, plus any retries. The 40-attempt budget is a ceiling, not a target.
`--limit` can reduce the company cap but cannot exceed `MAX_COMPANIES_PER_RUN`.

Successful recent companies are skipped for the freshness interval, so continuing
a run does not normally fetch those companies again. Unvisited companies are
prioritized, followed by the oldest discovery times. A limit or source error may
still leave pending work; a zero exit code alone is not a coverage audit.

To use a custom list, edit `config/companies.txt`: one bare ticker per line, with
optional `#` comments. The ticker must also resolve through
`config/kap_companies.json`; adding a ticker line alone does not create its KAP
member ID and exact company title. Review title aliases when issuers are renamed.

### Freshness, years, and source restrictions

- For annual data, start with at most one normal full-list run per day when
  updates are needed. Historical datasets usually need less frequent collection.
- Run one crawler at a time. The application also has a shared run lock.
- Do not add `--refresh` routinely. `fresh` means every requested fiscal year was
  checked for that company within `DISCOVERY_INTERVAL_HOURS`, whatever the answer
  was (`published`, `already_parsed`, `no_filing`, `unsupported`, `failed`). A
  year with no row is an answer too; asking again the next hour returns the same
  empty answer and only spends the request budget. Changing `KAP_YEARS` makes the
  companies that lack the new years due immediately. `--refresh` ignores freshness.
- A parser upgrade does not re-download anything. Run `reprocess` (zero HTTP) to
  re-parse the stored workbooks; companies fetched later because their interval
  elapsed are re-parsed on the spot.
- Rejected export rows (`UnknownTicker`) mean the source printed a title the
  registry does not know: a historical name. Run `verify-aliases` (next section).
- HTTP 429 records a cooldown of at least one hour, extended by `Retry-After` when
  appropriate. The run stops; it does not sleep for that hour inside the container.
- HTTP 401/403 or a detected security challenge records a persistent host block.
  Investigate access before continuing. Do not delete `data/state/` or switch
  proxies merely to bypass the restriction.
- Temporary network/server errors have bounded retries. An HTTP 666 on the export
  endpoint is an error to investigate; do not assume it is harmless because it was
  previously seen on `robots.txt`.

The export interface is undocumented in this project. Review the provider's
current access conditions; pacing and a proxy do not guarantee permission or
immunity from blocking.

### Historical company titles: `verify-aliases`

The export prints each row under the title the issuer had when that report was
published, so a renamed company (ARSAN, TRALT, BSOKE, GARFL, …) returns rows the
registry cannot attribute; they are listed as *rejected rows* in the summary and
the years read as `no_filing`. `verify-aliases` proves the titles from the source
itself: it requests the export for **one** company ID at a time, so every row it
gets back belongs to that ID whatever title it carries. Each proven title goes to
`config/kap_aliases.json` with the request, workbook hash and notification IDs as
evidence, and the company is made due again.

```bash
bash scripts/crawler-ubuntu.sh docker verify-aliases --max-requests 20
python3 scripts/review-run.py
bash scripts/crawler-ubuntu.sh docker sync --company-file /app/config/companies_candidates.txt
```

Candidates come from the newest sync summary: tickers that shared a batch with a
rejected title and have no row for the years it was rejected for, best-constrained
first. Rerun until it prints `nothing to verify`; each invocation is capped by
`--max-requests` (default 10) and by `MAX_REQUESTS_PER_RUN`. Tickers that return no
rows at all are recorded as `verified_empty` and not asked again for those years.
A title the registry already assigns to a different ticker is reported as a
**conflict** and never written; fix the seed by hand. `--tickers X,Y` verifies an
explicit selection instead. Titles that differ from the registry only in KAP's
own spelling (`SANAYİ`/`SANAYİİ`, `İ`/`I`, a hyphen, a dropped `VE`) are matched
without a request and listed in the summary as `normalized_title_matches`.

## SQL and Grafana

### Query SQL directly without an interactive SQL prompt

This command prompts for `MSSQL_READER_PASSWORD` and prints the results immediately.
Use `localhost` exactly, not `locahost`. `-Q` executes the query and exits, so you
do not need to type `GO` or paste into numbered SQL prompts.

```bash
docker compose exec mssql /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U fundamentals_reader -d StockFundamentals -C -W -b \
  -Q "SELECT ticker, fiscal_year, currency_code, consolidation_scope, revenue, net_profit, total_assets, total_equity, current_liabilities, non_current_liabilities FROM dbo.vw_fundamentals WHERE market_source = 'kap_compare' AND ticker IN ('THYAO','ASELS') AND fiscal_year IN (2024,2025) ORDER BY ticker, fiscal_year;"
```

Use your configured database name if it differs from `StockFundamentals`. To check
coverage across the collected list:

```bash
docker compose exec mssql /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U fundamentals_reader -d StockFundamentals -C -W -b \
  -Q "SELECT fiscal_year, currency_code, COUNT(DISTINCT ticker) AS companies, COUNT(*) AS statement_rows FROM dbo.vw_fundamentals WHERE market_source = 'kap_compare' AND fiscal_period = 4 GROUP BY fiscal_year, currency_code ORDER BY fiscal_year, currency_code;"
```

The view can contain both consolidation scopes, so statement rows are not always
one per company/year. Compare coverage with the requested list and run errors.

### Use the dashboard

The repository provisions **Stock Fundamentals MVP** in the **Fundamentals** folder.
Its configuration sets `allowUiUpdates: false`; it is managed through files.
The separately supplied **KAP Fundamentals Explorer** JSON is not included in this
source archive. If you have that JSON:

1. Open [Grafana Import](http://127.0.0.1:3000/dashboard/import).
2. Upload `KAP_Fundamentals_Explorer.json`.
3. Select the existing **StockFundamentals** datasource and click **Import**.
4. Select currency `TRY`, company `All`, and fiscal year `All`, then refresh.

This creates a separate editable dashboard. Do not copy its import JSON directly
into the provisioning directory without first resolving its datasource input.
See [Grafana's import instructions](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/import-dashboards/).

The datasource connects to `mssql:1433` as `grafana_reader`, using
`MSSQL_GRAFANA_PASSWORD`. Its hostname is the Compose service name, even though
native host SQL clients use `127.0.0.1:1433`. Keep Grafana on the Compose bridge.

For a successful THYAO/ASELS 2024–2025 pilot, Explorer should show 2 companies,
4 selected annual records and 2 years. Do not expect every candidate company to
have the same coverage.

### What the financial values mean

The export requests ten fields: revenue, net profit, owners' profit, NCI profit,
total assets, total equity, current liabilities, non-current liabilities,
liabilities plus equity, and finance-sector revenue. Some fields are inapplicable
or missing for particular companies.

- Monetary values are already stored in base currency units. Do not apply
  `currency_scale` again. Missing values are `NULL`, not zero.
- EPS, operating income, cash, debt, EBITDA, free cash flow and shares outstanding
  are not populated by this ten-item export. An `unavailable` label for those
  metrics is not necessarily a connection problem.
- `dbo.vw_fundamentals` selects the latest valid, nonwithdrawn, noncomparative
  version per company/year/period/consolidation scope. Historical versions remain
  in the underlying tables; the view does not return every stored version.
- `dbo.vw_latest_fundamentals` selects one latest row per company from that view,
  preferring consolidated scope when period-end dates match.
- This source supplies delayed current-period columns, without exact period dates,
  prior-period restatements or withdrawal detection. Jan–Dec dates are inferred
  for supported calendar-year issuers. The data is not sufficient for point-in-time
  backtesting; cross-year inflation and restatement adjustments are not harmonized.

## Read run results

| Result | Interpretation |
|---|---|
| `published` | A parsed report was processed; inspect `persist` for the database outcome. |
| `already_parsed` | This stored version was already parsed successfully. |
| `fresh` | Recent discovery caused the company to be skipped; zero new HTTP is normal. |
| `deferred` | The company cap left this company for a later run. |
| `no_filing` | The source returned no row for that company and fiscal year. Counts as checked. |
| `unsupported` | Bank, insurance or finance-sector statement format: not mapped. GENERAL and HOLDING formats are. |
| `failed` | The row is missing a required value (e.g. no revenue line at all). Counts as checked. |
| `unresolved`, `error` | Identity or fetch/storage problem; the company is retried on the next run. |
| `stopped` | Review the budget, cooldown or block reason and pending list. |

The terminal header's “companies” count actually counts summary entries: one
company can have an entry for each fiscal year. Four entries for two companies
and two years do not mean four distinct companies.

Run summaries are written to `data/runs/<run-id>/summary.json` on the host.
The terminal's `/app/data/...` path is the same bind-mounted directory inside the
container. Print a compact summary of the newest run with:

```bash
python3 - <<'PY'
from pathlib import Path
from collections import Counter
import json

paths = list(Path('data/runs').glob('*/summary.json'))
if not paths:
    raise SystemExit('No run summary found.')
path = max(paths, key=lambda p: p.stat().st_mtime_ns)
data = json.loads(path.read_text())
print('Summary:', path)
print('HTTP attempts:', data.get('request_attempts'))
print('Statuses:', dict(Counter(r.get('status', '?') for r in data.get('companies', []))))
print('Stopped reason:', data.get('stopped_reason') or 'none')
for row in data.get('companies', []):
    if row.get('error'):
        print(row.get('ticker'), row.get('status'), row['error'])
PY
```

CLI sync exit codes are `0` for no classified company failure, `1` for classified
company failures, `2` for a stopped run/configuration error, and `3` for an active
run lock. Inspect summaries and SQL coverage even after exit `0`.

## Troubleshooting

### Browser works, crawler cannot reach KAP

A loopback SSH proxy listens only on the host. On bridge networking,
`host.docker.internal` resolves to a host gateway address; that does not expose a
listener bound only to `127.0.0.1`. The included `compose.proxy-host.yml` runs only
the crawler with host networking, allowing it to reach the host's loopback proxy.
See [Docker host networking](https://docs.docker.com/engine/network/drivers/host/).

Use `bash scripts/crawler-ubuntu.sh docker ...`, not the unmodified `make sync` or
`make probe`, for this Ubuntu SOCKS setup. A timeout alone does not prove whether
DNS, the proxy, TCP, or the destination caused the failure. Compare the listener,
explicit host-proxy curl, and crawler probe from the installation section.

The final crawler image does not contain `curl`. Do not run it with
`--entrypoint curl`; use the host's curl or the included Python probe.

For a different proxy or published SQL port, export values in the terminal before
running both the helper and Compose commands:

```bash
export CRAWLER_PROXY_URL=socks5h://127.0.0.1:10808
export CRAWLER_DB_HOST_PORT=1433
```

The helper supplies explicit upper/lowercase proxy values. Setting a conflicting
proxy only in `.env` will not override the helper's defaults. Do not use
`docker-compose.yml` as the helper's mode: valid modes are `db`, `docker`, `native`.

### Grafana address or port conflict

```bash
docker ps -a --filter name=grafana --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
sudo ss -ltnp 'sport = :3000'
docker compose logs --tail=50 grafana
```

The expected published port is `127.0.0.1:3000->3000/tcp`. Empty ports on a bridge
container do not expose Grafana to the host. Recreate this project's Grafana with
the three-file command in installation step 5. If another Grafana owns port 3000,
identify it before stopping it; do not remove its volume or dashboards by accident.
Bypass the browser proxy for localhost. This address is local to the Ubuntu machine.

### Grafana certificate or login failure

The existing `GODEBUG=x509negativeserial=1` setting permits the observed negative
certificate serial format while retaining TLS. It is a local compatibility
workaround; a correctly issued SQL certificate is the durable fix. It applies to
the Grafana process. See [Go compatibility settings](https://go.dev/doc/godebug).

If the error persists after an older container was reused, recreate Grafana as in
step 5 and inspect its setting:

```bash
docker compose exec -T grafana printenv GODEBUG
```

Login failures are separate: SQL terminal access uses `MSSQL_READER_PASSWORD`,
Grafana's SQL datasource uses `MSSQL_GRAFANA_PASSWORD`, and Grafana web login uses
`GF_SECURITY_ADMIN_PASSWORD`. Changing `.env` alone does not rotate existing
accounts. Missing optional plugin directories or failed internet update checks
also do not by themselves establish that the local SQL connection is broken.

## Import existing downloads without KAP requests

Copy a previously downloaded manifest into `imports/`. This example prompts for
the file path instead of assuming a particular manifest filename:

```bash
read -r -p 'Downloaded manifest JSON path: ' crawler_manifest
cp -- "$crawler_manifest" imports/manual-manifest.json
bash scripts/crawler-ubuntu.sh docker import-kap-export \
  --input /app/imports/manual-manifest.json
```

To validate before opening SQL, add `--dry-run --output /app/data/validation.json`.
Dry-run can still archive files locally. Standalone XLSX input also requires an
explicit `--tickers` selection to verify company identities. Reimporting identical
values does not create duplicate report versions. Legacy manifests lack an
original download timestamp, so import time is recorded in their provenance.

The browser exporter is another supported collection route when source access is
allowed in your browser. It is not a way to bypass an access restriction:

```bash
bash scripts/crawler-ubuntu.sh docker build-kap-script \
  --tickers THYAO,ASELS --years 2024,2025 \
  --output /app/config/export_pilot.js
```

Open the [KAP comparison page](https://www.kap.org.tr/en/kalem-karsilastirma), paste
`config/export_pilot.js` in the developer console, and use `kapProgressStatus()`,
`kapStop()`, `kapResume()`, or `kapDownloadSoFar()`. The generated exporter has its
own request budget, browser storage, and stop state; Python `.env` pacing does not
configure it. Download the manifest and retain it before importing.

## Backups, reinstalling and updates

### Preserve collected data

With SQL Server running and no crawl active:

```bash
bash scripts/backup.sh
```

The backup directory contains a SQL `.bak`, `raw-and-state.tgz`, and checksums.
It does not include `.env`, company configuration, run summaries, or the Grafana
volume. Preserve configuration/credentials securely, copy backups off the machine,
and export any manually imported dashboards as JSON. Never commit credentials.

`scripts/restore.sh` accepts a backup directory and replaces the target database
and raw/state directories. Use it only for an intentional restore, not ordinary
startup. It invokes `init-db` afterwards to establish logins and user mappings.
Copying the project folder alone does not copy SQL/Grafana named volumes.

### Reuse working images

To reduce dependence on image/package downloads during a later reinstall, save
these images while the current installation is working:

```bash
mkdir -p backups
docker image save -o backups/stock-crawler-images.tar \
  stock-crawler:local \
  mcr.microsoft.com/mssql/server:2022-CU16-ubuntu-22.04 \
  grafana/grafana-oss:11.3.0
```

On another compatible AMD64 Docker host, copy and load that file:

```bash
docker image load -i backups/stock-crawler-images.tar
```

Matching loaded images let you skip the pull/build step when reusing the same
application version. This archive contains images only, not SQL data or Grafana
settings. Rebuild the crawler when its Python code or dependencies change.

### Existing Makefile shortcuts

The supplied Makefile uses only the base Compose file. Its `sync`/`probe` commands
do not select the Ubuntu host-proxy override. Use this README's helper commands
for that setup. Running bare `make` invokes the first target, **setup**; it does
not print help. To see crawler help:

```bash
bash scripts/crawler-ubuntu.sh --help
bash scripts/crawler-ubuntu.sh docker --help
```

For ordinary shutdown, keep volumes:

```bash
docker compose -f docker-compose.yml -f compose.db-local.yml -f compose.proxy-host.yml down
```

Do not add `-v` when you intend to preserve database and Grafana volumes.

## Native Python and development

Docker is the primary installation path above. The optional `native` helper mode
expects `.venv-native/bin/stock-crawler` and uses the same local SQL port, proxy,
and project data directory. See [UBUNTU-PROXY.md](UBUNTU-PROXY.md) for native setup,
including Python 3.12 and Microsoft ODBC Driver 18. Merely selecting `native`
does not install its virtual environment.

After preparing that environment:

```bash
bash scripts/crawler-ubuntu.sh db
bash scripts/crawler-ubuntu.sh native sync --tickers THYAO,ASELS
```

For offline development tests in an environment with the package's system
prerequisites installed:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
node --test tests/browser_core.test.cjs
```

Node 18+ is optional for the browser-core tests. SQL integration tests require
`STOCK_CRAWLER_TEST_MSSQL_URL` pointing at a disposable database. Test results from
another environment do not verify your installation's live KAP access.

## Project layout

| Path | Purpose |
|---|---|
| `src/stock_crawler/kap_export.py` | Batched export client, registry and XLSX parsing |
| `src/stock_crawler/aliases.py` | `verify-aliases`: historical titles proven by single-company exports |
| `src/stock_crawler/client_export.py` | `export-company-fundamentals`: offline review against the client contract |
| `src/stock_crawler/fetch.py` | Pacing, retries, budgets, cooldowns and blocks |
| `src/stock_crawler/pipeline.py` | Discovery, freshness, parsing and persistence |
| `src/stock_crawler/storage.py` | Raw files, summaries, state and run locking |
| `src/stock_crawler/db.py` | SQL repository and initialization |
| `sql/schema.sql` | Tables and views |
| `config/companies.txt` | Active ticker selection |
| `config/companies_candidates.txt` | Larger dated candidate list |
| `config/kap_companies.json` | KAP identities and hand-verified title aliases |
| `config/kap_aliases.json` | Titles proven by `verify-aliases` (generated; commit it) |
| `data/raw/_exports/` | Original workbooks addressed by SHA-256 |
| `data/raw/kap_compare/` | Per-record snapshots and parser output |
| `data/runs/` | Run summaries |
| `grafana/provisioning/` | SQL datasource and dashboard provisioning |
| `scripts/crawler-ubuntu.sh` | Ubuntu Docker/native launcher |
| `scripts/review-run.py` | Summarise the newest run; `--write-pending FILE` |

See [REVIEW-2026-09-21.md](REVIEW-2026-09-21.md) for the September 19 run analysis and the
decisions behind per-year freshness, HOLDING support, title normalization and the client
export's scope policy; [FIXES-2026-09-19.md](FIXES-2026-09-19.md) and [REVIEW.md](REVIEW.md)
for earlier ones.
