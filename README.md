# stock-crawler

Crawls Borsa İstanbul and Türkiye market data from official and public sources and writes
one CSV per database table: listed companies, daily share prices, quarterly financial
statements, BIST index closes and sovereign macro data. `load_db` then loads those CSV files
into SQL Server.

```bash
python -m stock_crawler market_data --years 5
```

- One command line, one settings file (`config/config.toml`), one virtualenv.
- Values are copied as each source publishes them: no unit conversion, no rounding, no recoding.
- Choose how much history to fetch with `years` (e.g. 1, 2, 5 or 10).
- Every source address is in the settings file.
- Downloads are cached, so an interrupted run resumes and later runs fetch only what is new.
- Each crawler adapts its speed to its server and backs off when refused, so your IP is not blocked.

## Contents

1. [Quick start](#1-quick-start)
2. [What each command collects](#2-what-each-command-collects)
3. [Project layout](#3-project-layout)
4. [Configuration](#4-configuration)
5. [Running](#5-running)
6. [Data sources and addresses](#6-data-sources-and-addresses)
7. [Output files and columns](#7-output-files-and-columns)
8. [Staying under the servers' limits](#8-staying-under-the-servers-limits)
9. [Loading into SQL Server](#9-loading-into-sql-server)
10. [Troubleshooting](#10-troubleshooting)
11. [Changing or adding sources](#11-changing-or-adding-sources)
12. [Tests](#12-tests)

## 1. Quick start

You need Python 3.10 or newer, and `curl` (preinstalled on macOS and most Linux systems).
Run every command from the project folder.

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m stock_crawler --help       # list the commands
python -m stock_crawler sovereign    # a quick first run (~1 minute)
```

Before loading data into your database, set your own IDs in the config (see
[IDs](#ids-that-must-match-your-database)).

On Ubuntu, first install `sudo apt install -y python3 python3-venv curl ca-certificates`,
or run `deploy/setup_ubuntu.sh`, which also sets up SQL Server access (see
[section 9](#on-ubuntu-set-up-test-schedule)).

## 2. What each command collects

| Command | Table(s) | Output | Source | First run | Later runs |
|---|---|---|---|---|---|
| `companies` | Company | `output/companies/companies.csv` | KAP + Borsa İstanbul | ~45 min | ~1 min within 7 days (pages cached), else ~45 min |
| `market_data` | MarketData | `output/market_data/market_data.csv` (+ one CSV per ticker) | Borsa İstanbul daily bulletin + TCMB | ~1–1.5 h for 5 years | seconds (new days only) |
| `check_market_data` | – | report on screen | reads `market_data.csv` | seconds | seconds |
| `fundamentals` | CompanyFundamental | `output/fundamentals/company_fundamental.csv` (+ raw items, companies) | KAP | ~3 h for 5 years, ~6 h for 10 | ~1 h (recent years) |
| `fundamental_reports` | CompanyFundamental | fills 7 columns of `company_fundamental.csv` (+ `output/fundamental_reports/report_lines.csv`) | KAP full financial reports | ~30 min for the 10 years' quarter files, then ~6 h for the ~450 statements they lack | seconds to minutes (new statements only) |
| `market_index` | Market, MarketIndexMaster, MarketIndexData | `output/market_index/*.csv` | İş Yatırım | ~2 min | ~2 min |
| `sovereign` | MacroSovereign | `output/sovereign/macro_sovereign_tr.csv` | CBRT EVDS + Treasury | ~1 min | ~1 min |
| `load_db` | every table in `config/db_tables.csv` | your SQL Server database | the CSV files above | – | – |
| `all` | the commands in `run_all` | | | | |

`market_data` reads its ticker list from `companies.csv`, so run `companies` first, and
`fundamental_reports` fills the CSV that `fundamentals` writes, so run it after. `all` does
this for you. Add `load_db` at the end of `run_all` to load the results into SQL Server
(see [section 9](#9-loading-into-sql-server)).

## 3. Project layout

```
stock-crawler/
├── README.md
├── requirements.txt           one dependency list for every crawler
├── requirements-dev.txt       + pytest
├── deploy/
│   ├── setup_ubuntu.sh        Ubuntu: Python, ODBC driver for SQL Server, .venv, tests
│   ├── create_tables.sql      the SQL Server tables load_db fills, for a new database
│   └── restore_db.sh          restores a StockDb backup (.bak) into the Docker SQL Server
├── config/                    everything you are expected to edit
│   ├── config.toml            source URLs, period (years), IDs, output paths
│   ├── markets.csv            rows for the Market table (market_index)
│   ├── indices.csv            rows for the MarketIndexMaster table (market_index)
│   └── db_tables.csv          which CSV goes into which SQL Server table (load_db)
├── stock_crawler/             the code: one sub-package per command
│   ├── cli.py                 python -m stock_crawler: reads the config, runs commands
│   ├── http_client.py         HTTP client shared by companies and market_data
│   ├── dates.py
│   ├── companies/             crawler.py, parsers.py
│   ├── market_data/           crawler.py, check.py
│   ├── fundamentals/          crawler.py
│   ├── fundamental_reports/   crawler.py
│   ├── market_index/          crawler.py, models.py, export.py, http_client.py
│   ├── sovereign/             crawler.py, fields.py, evds_client.py, treasury.py, ...
│   └── load_db/               loader.py: CSV files -> SQL Server
├── tests/                     offline tests
├── backups/                  StockDb.bak backups (not version-controlled)
├── output/<command>/          generated CSV files      ┐
├── cache/<command>/           downloaded raw pages      ├ created by the crawlers,
└── logs/<command>.log         one log per command       ┘ not version-controlled
```

One name is used everywhere for each crawler: the command, the config section, the code
folder, and its `output/`, `cache/` and `logs/` entries.

Each crawler keeps its own HTTP handling. Each one is tuned to its server: KAP blocks
clients that ask too fast, while EVDS and İş Yatırım are relaxed.

## 4. Configuration

All settings are in `config/config.toml`. Use another file with
`python -m stock_crawler --config my.toml COMMAND`.

### The rule

Every key in a command's section is one of that command's command-line options, with `_`
instead of `-`. The command line wins over the file:

```toml
[companies]
market_id = 1        # same as: python -m stock_crawler companies --market-id 1
skip_details = true  # same as: --skip-details   (false or "" = use the built-in default)
```

```bash
python -m stock_crawler companies --market-id 7    # 7 wins over the 1 in the file
```

`python -m stock_crawler COMMAND --help` lists every option and its default. A misspelled
key fails at once with `unrecognized arguments`. Relative paths are relative to the project
folder. A flag set to `true` in the file can't be switched off on the command line; edit
the file instead.

### Period: `years`

```toml
years = 5            # e.g. 1, 2, 5 or 10
```

The top-level `years` applies to every command that fetches history. A section can override
it with its own `years`, and `--years N` on the command line overrides both. `--start` /
`--end` (YYYY-MM-DD) set an exact range instead.

| Command | What `years = N` means | Example: N = 5 on 2026-09-26 | Earliest data |
|---|---|---|---|
| `market_data` | from 1 January N years ago | 2021-01-01 → today | – (logs a warning if the first bulletin is later than asked) |
| `market_index` | from 1 January N years ago | 2021-01-01 → today | 2000 for XU100 |
| `fundamentals` | fiscal years from N years ago to the current year (1 January N years ago) | FY 2021 → FY 2026 | FY 2016 (KAP's item search) |
| `sovereign` | from 1 January N years ago; only periods that have ended | 2021-01-01 → last complete month/quarter/year | 20 years fills every column |
| `companies` | not used: always today's listed companies | | |

Every command starts on 1 January so the first year is complete: `years = 10` in 2026 means
2016-01-01 to today (ten full years plus this year so far).

Every run writes the whole requested period and replaces the previous CSV. The only
exception is `fundamentals --update`, which merges into it.

### Source addresses

Every URL a crawler uses is a setting. Change it here if a site moves, or to use a mirror.
The defaults:

```toml
[companies]
directory_url = "https://kap.org.tr/tr/bist-sirketler"
listing_url   = "https://www.borsaistanbul.com/datum/ilkislem.zip"

[market_data]    # {d:...} is replaced by the day, using Python date codes
bulletin_url = "https://www.borsaistanbul.com/data/thb/{d:%Y}/{d:%m}/thb{d:%Y%m%d}1.zip"
fx_url       = "https://www.tcmb.gov.tr/kurlar/{d:%Y%m}/{d:%d%m%Y}.xml"

[fundamentals]   # the API paths (/en/api/...) are appended to it
base_url = "https://www.kap.org.tr"

[market_index]
source_url = "https://www.isyatirim.com.tr/_Layouts/15/IsYatirim.Website/Common/ChartData.aspx/IndexHistoricalAll"

[sovereign]
evds_url     = "https://evds3.tcmb.gov.tr/igmevdsms-dis"
treasury_url = "https://www.hmb.gov.tr/portal/v2/pages"
```

Which EVDS *series* feeds each macro column is set in `stock_crawler/sovereign/fields.py`
(see [section 11](#11-changing-or-adding-sources)).

### IDs that must match your database

| Setting | Where | Written to |
|---|---|---|
| `market_id` | `[companies]` in config.toml | `companies.csv` → `MarketId` |
| `market_id` | `[sovereign]` in config.toml | `macro_sovereign_tr.csv` → `MarketId` |
| `MarketId` | `config/markets.csv` | `market.csv`, and `market_index_master.csv` via indices.csv |
| `IndexId` | `config/indices.csv` | `market_index_master.csv`, `market_index_data.csv` |
| `CompanyId` | your own `company_ids.csv` (optional) | `market_data --db-output`, see [section 9](#9-loading-into-sql-server) |

The shipped values all use MarketId 1 (Borsa İstanbul, Türkiye): `market_id` for companies
and for sovereign, and `markets.csv`. Change all three together if your database uses
another ID; a foreign key fails when they differ. `fundamentals` uses KAP's own company
code as `CompanyId` (see [section 9](#configdb_tablescsv)).

### Other settings

```toml
run_all = ["companies", "market_data", "market_index", "sovereign", "fundamentals", "fundamental_reports"]  # used by `all`, in order
log_dir = "logs"
```

## 5. Running

```bash
python -m stock_crawler COMMAND [options]          # one command
python -m stock_crawler all [--years N] [-v]       # every command in run_all, one after another
python -m stock_crawler COMMAND --help             # all options of a command
```

`-v` / `--verbose` gives debug logging. Logs go to the screen and are appended to
`logs/<command>.log`.

### Examples

```bash
# Companies
python -m stock_crawler companies                           # full (~45 min)
python -m stock_crawler companies --skip-details            # ~1 min, no SectorName/ReportingCurrency

# Daily prices
python -m stock_crawler market_data                         # `years` from the config
python -m stock_crawler market_data --years 10
python -m stock_crawler market_data --start 2020-01-01 --end 2024-12-31 --all-equities   # incl. delisted
python -m stock_crawler check_market_data                   # check the result

# Financial statements
python -m stock_crawler fundamentals                        # `years` from the config
python -m stock_crawler fundamentals --update               # previous + current year, merged into the CSV
python -m stock_crawler fundamentals --symbols ASELS,THYAO --start 2023-01-01
python -m stock_crawler fundamental_reports                 # then fill Ebitda, debt, cash, FCF, EPS, shares
python -m stock_crawler fundamental_reports --symbols ASELS,THYAO --years 1

# Index closes
python -m stock_crawler market_index                        # all 54 indices
python -m stock_crawler market_index -i XU100 XU030 --years 1

# Macro data
python -m stock_crawler sovereign
python -m stock_crawler sovereign --years 20 --output output/sovereign/macro_sovereign_tr_20y.csv
python -m stock_crawler sovereign --cds-csv config/tr_cds_5y.csv
python -m stock_crawler sovereign --list-fields             # where each column comes from

# Into SQL Server
python -m stock_crawler load_db --dry-run                   # load and count, then roll back: changes nothing
python -m stock_crawler load_db                             # insert new rows, update changed ones
python -m stock_crawler load_db --tables MarketIndexData MacroSovereign

# Everything
python -m stock_crawler all
python -m stock_crawler all --years 10
```

`all` runs the commands one after another, never in parallel, so KAP never sees two
crawlers at once. If one command fails, `all` goes on with the next and prints a summary.
It exits with 0 only if every command succeeded.

### Long runs and scheduling

On macOS, keep the machine awake during a long first run:
`caffeinate -i python -m stock_crawler all`. On a server, use `tmux` or
`nohup ... &`.

A daily update after the market closes (the bulletin appears around 18:30 Istanbul time).
Cron uses the server's time zone:

```cron
0 20 * * 1-5  cd /opt/stock-crawler && .venv/bin/python -m stock_crawler all >> logs/cron.log 2>&1
```

Or schedule them separately. For example, prices and indices daily, the rest weekly:

```cron
0 20 * * 1-5  cd /opt/stock-crawler && .venv/bin/python -m stock_crawler market_data  >> logs/cron.log 2>&1
15 20 * * 1-5 cd /opt/stock-crawler && .venv/bin/python -m stock_crawler market_index >> logs/cron.log 2>&1
0 2 * * 6     cd /opt/stock-crawler && .venv/bin/python -m stock_crawler companies    >> logs/cron.log 2>&1
0 4 * * 6     cd /opt/stock-crawler && .venv/bin/python -m stock_crawler sovereign    >> logs/cron.log 2>&1
0 5 * * 6     cd /opt/stock-crawler && .venv/bin/python -m stock_crawler fundamentals --update >> logs/cron.log 2>&1 \
              && .venv/bin/python -m stock_crawler fundamental_reports >> logs/cron.log 2>&1
```

Check the exit code before loading data from a scheduled run.

### PyCharm

Set the project interpreter to `.venv`. Create a Python run configuration with **Module name**
`stock_crawler`, **Parameters** e.g. `market_data --years 1`, and the project folder as
**Working directory**.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | failed (see the log); for `market_index`: some indices failed, the others were written; for `check_market_data`: errors found in the CSV; for `load_db`: some tables failed and were rolled back, the others were loaded |
| 2 | bad settings or options; for `companies` / `market_data`: some pages or days could not be fetched, so the CSV was **not** written (rerun later, or pass `--allow-incomplete`) |
| 3 | stopped early because the server kept refusing (`fundamentals`, `fundamental_reports`, `market_index`); what was fetched so far is written. Rerun later; `fundamentals` and `fundamental_reports` resume from their cache |
| 130 | interrupted (Ctrl-C or `kill`); caches are kept |

## 6. Data sources and addresses

| Command | Provider | Address | What is taken |
|---|---|---|---|
| companies | KAP – Public Disclosure Platform | `https://kap.org.tr/tr/bist-sirketler` | tickers, legal names, profile links |
| companies | KAP company pages | `https://kap.org.tr/tr/sirket-bilgileri/ozet/<id>`, `.../tr/sirket-finansal-bilgileri/<id>` | sector, reporting currency |
| companies | Borsa İstanbul | `https://www.borsaistanbul.com/datum/ilkislem.zip` | which codes are shares (`.E`), listing date |
| market_data | Borsa İstanbul Equity Market daily bulletin | `https://www.borsaistanbul.com/data/thb/YYYY/MM/thbYYYYMMDD1.zip` | OHLCV, VWAP, trades, corporate-action flags for every share |
| market_data | Central Bank of Türkiye (TCMB) | `https://www.tcmb.gov.tr/kurlar/YYYYMM/DDMMYYYY.xml` | USD/TRY forex buying rate |
| fundamentals | KAP Financial Statement Item Search | `https://www.kap.org.tr/en/kalem-karsilastirma` and its API (below) | quarterly statement items |
| fundamental_reports | KAP home page "Financial Statements" download, no company chosen | `https://www.kap.org.tr/en/api/financialTable/download/{year}/{quarter}` | every company's full financial report for one quarter, one zip |
| fundamental_reports | KAP disclosure "EXCEL" export | `https://www.kap.org.tr/en/api/notification/export/excel/{NotificationId}` | the full financial report of one statement (for the few the zip lacks) |
| market_index | İş Yatırım (brokerage) | `https://www.isyatirim.com.tr/_Layouts/15/IsYatirim.Website/Common/ChartData.aspx/IndexHistoricalAll?period=1440&from=…&to=…&endeks=XU100` | daily index closes |
| sovereign | CBRT EVDS | `https://evds3.tcmb.gov.tr/igmevdsms-dis` (`GET /serieList/fe/…`, `POST /fe`) | GDP, tax revenue, policy rate, CPI, USD rate |
| sovereign | Ministry of Treasury and Finance | `https://www.hmb.gov.tr/portal/v2/pages?slug=kamu-finansmani-istatistikleri` → Excel link | central government debt stock |

**KAP item-search API** (`fundamentals`), the same public, login-free calls as the web page:

| Step | Request | Returns |
|---|---|---|
| 1 | `GET /en/api/company/items/IGS/A` | listed companies: stock code, name, KAP company code, member id |
| 2 | `GET /en/api/analysis/companies-by-sector/{SECTOR}` | which companies file which statement type (GENERAL, HOLDING, BANKS, PAR-BANKS, INSURANCE, FINANCE) |
| 3 | `GET /en/api/analysis/compare-items-by-sector/{SECTOR}` | the item catalogue (XBRL ids such as `ifrs-full_Revenue`) |
| 4 | `POST /en/api/export/compareItems` | the XLSX export, at most 10 companies × 2 years × 10 items |

**EVDS series** (`sovereign`; the code checks them before every run):

| Column | Source | Series | Page |
|---|---|---|---|
| Gdp | EVDS (TurkStat) | `TP.GSYIH20.BY.B1GQ`: GDP, expenditure approach, current prices | [bie_gsyhhrccar](https://evds3.tcmb.gov.tr/tumSeriler/150201/bie_gsyhhrccar) |
| TaxRevenue | EVDS (MoTF) | `TP.KB.GEL003`: central government budget, I. Taxes | [bie_kbmgel](https://evds3.tcmb.gov.tr/tumSeriler/1503/bie_kbmgel) |
| PublicDebt | Treasury | Excel *Merkezi Yönetim Borç Stoku Enstrüman Dağılımı*, column "TOPLAM STOK" | [hmb.gov.tr statistics](https://www.hmb.gov.tr/kamu-finansmani-istatistikleri) |
| InterestRate | EVDS (BIS) | `TP.BISPOLFAIZ.TUR`: CBRT policy rate | [bie_bispolfaiz](https://evds3.tcmb.gov.tr/tumSeriler/550202/bie_bispolfaiz) |
| Cpi | EVDS (TurkStat) | `TP.TUKFIY2025.GENEL`: CPI, 2025=100 | [bie_tukfiy2025](https://evds3.tcmb.gov.tr/tumSeriler/2005/bie_tukfiy2025) |
| FxRateUsd | EVDS | `TP.DK.USD.A.YTL`: CBRT indicative USD buying rate | [bie_dkdovytl](https://evds3.tcmb.gov.tr/tumSeriler/2501/bie_dkdovytl) |
| CdsSpreadBps | your own file (`--cds-csv`) | Türkiye 5Y USD CDS (paid market data; no free official source) | – |

### Terms of use

- **KAP, Borsa İstanbul, TCMB/EVDS and the Treasury** are official public sources. When you
  publish the macro data, cite CBRT EVDS and the Ministry of Treasury and Finance, as the
  EVDS terms require.
- **İş Yatırım** (`market_index`) is an unofficial endpoint that can change without notice.
  Its `robots.txt` disallows `/_layouts/`, and the endpoint lives under `/_Layouts/`.
  Check İş Yatırım's terms before production or commercial use. The official, licensed
  source is [Borsa İstanbul Datastore](https://datastore.borsaistanbul.com/).
- The crawlers identify themselves (`TurkeyCompanyDirectory/2.0`, `sovereign-crawler/1.0`),
  except `fundamentals` and `market_index`, which send a browser user agent like the web
  pages they call. `fundamental_reports` uses the same client and name as `companies`.

## 7. Output files and columns

All files are UTF-8 CSV with a header row. They are written to a temporary file first and
then renamed, so a crash never leaves a half-written file. No values are guessed: anything a
source does not publish stays empty.

**Values are copied as published.** Nothing is converted to other units, rounded or recoded.
A value keeps the source's unit (e.g. `1000TL` statements, thousand-TRY GDP) and every digit
the source sends. Only the representation is made loadable:
- Dates are written as `YYYY-MM-DD` (from Excel date cells, `dd-mm-yyyy` text, timestamps, and
  the `21.05.2020` style of twelve Borsa bulletins from May–June 2020).
- KAP's Turkish number text is read as a number (`1.166.684` → `1166684`, `-1,5` → `-1.5`);
  otherwise SQL would read `1.166.684` as 1.166.
- The share code `AKBNK.E` is written as the ticker `AKBNK`, to match `companies.csv`.

Calculated columns stay, computed from the raw values: `CloseUsd`, the fundamentals table
columns (e.g. Revenue = revenue + finance-sector revenue), and sovereign quarterly/annual
figures. Rows are still filtered as before: untraded days, stray weekend index bars, and
incomplete periods are left out.

### companies → `output/companies/companies.csv`

| Column | Source / meaning |
|---|---|
| `Ticker`, `FullName` | KAP directory. Only codes that Borsa İstanbul lists as shares (`.E` in `ilkislem.xlsx`) are kept; KAP's debt, sukuk and leasing issuer codes (e.g. `ACP`) are dropped. |
| `MarketId` | the `market_id` setting; empty if not set |
| `SectorName` | KAP profile, "Şirketin Sektörü"; several labels joined with `; ` |
| `ReportingCurrency` | latest "Sunum Para Birimi" on KAP's financial summary (statement currency, not trading currency), as published: `TL`, `1000TL`, `USD`, … |
| `IsActive` | `1`: in today's KAP directory |
| `IpoDate` | Borsa İstanbul listing date |
| `IpoDateSource` | `listing_date`, or `first_trading_day` for old (mostly pre-1990) listings where Borsa only publishes that |

If some KAP pages still fail after retries and cool-downs, the CSV is **not** overwritten.
Rerun later (fetched pages are reused), or pass `--allow-incomplete`.

### market_data → `output/market_data/`

`market_data.csv` (all tickers), `prices/<TICKER>.csv` (one per ticker, setting `split_dir`),
and optionally `market_data_db.csv` (see section 9).

| Column | Meaning |
|---|---|
| `Ticker`, `Date` | share code without `.E`; trade date |
| `Open`, `High`, `Low`, `Close` | continuous-session prices in TRY, **not adjusted** for splits, bonus issues or dividends |
| `Vwap` | volume-weighted average price |
| `Volume`, `TradedValue`, `Trades` | shares traded, TRY value, number of trades |
| `PreviousClose`, `ChangePercent` | Borsa's reference previous close and the % change to it |
| `CorporateAction` | Borsa's corporate-action code on ex-dates (bonus/rights issues, dividends); empty otherwise |
| `UsdTry` | TCMB USD forex buying rate that day. On a TCMB holiday that is a trading day, the last published rate, which TCMB defines as valid until the next one |
| `CloseUsd` | `Close / UsdTry` |

- Only days on which a share traded are written. Rows are sorted by ticker, then date.
  `no_usd = true` drops the two USD columns.
- **Prices are raw.** Bonus and rights issues (bedelsiz/bedelli) show up as large one-day
  drops on days with a `CorporateAction` flag; e.g. AKFYE on 2026-09-21 (`03`, 22 → 3.49).
  Adjust with KAP's published ratios, or exclude those days, before computing returns.
- Tickers without trades in the period are listed as a warning. Use `--all-equities` for
  research that must include delisted companies (avoids survivorship bias).
- One bulletin covers every share for one day, so 5 years is ~1,300 bulletins plus ~1,300
  small TCMB files, however many tickers you want. They never change and stay cached. A
  weekday without a bulletin older than 7 days is remembered as a holiday.

`check_market_data` reports **errors** (broken file: bad header, duplicates, Low > High,
USD mismatch; exit code 1) and **warnings** worth a look (big moves without a corporate
action flag, PreviousClose gaps, thin days, tickers without data).

### fundamentals → `output/fundamentals/`

| File | Content |
|---|---|
| `company_fundamental.csv` | one row per `(CompanyId, FiscalYear, FiscalQuarter)`, CompanyFundamental columns in table order, then `PresentationCurrency` (`--with-meta` appends StockCode, CompanyName, …) |
| `company_fundamental_raw.csv` | every fetched KAP item (long format) with notification id, currency, consolidation |
| `companies.csv` | `CompanyId` ↔ stock code, name, KAP member oid, statement sector |

- `CompanyId` is KAP's numeric company code.
- Values are in the statement's own unit, given in `PresentationCurrency` as KAP writes it:
  `TL`, `1000TL`, `1000000TL`, `USD`, … A `1000TL` value of `496504` means 496,504,000 TL.
  The raw CSV also has a `Multiplier` column (1, 1000, …).
- Your table needs a `PresentationCurrency` column to load this file as is.
- Income-statement items are **year-to-date**, as KAP publishes them (Q2 = 6 months,
  Q4 = full year). When consolidated and solo statements both exist, consolidated wins.
- These are the "current period" figures of the latest statement for each period; later
  restatements of prior periods are not reflected.
- The config fetches 10 years (FY 2016, KAP's first year, to today), **newest first**:
  2025–2026 for every sector, then 2023–2024, and so on. A run stopped by KAP already has
  the recent years. `--update` asks for the same 2025–2026 pair, so it reuses that cache.
- `PeriodEndDate` is derived from Year + Period; `PublishDate` is the date part of KAP's
  "Publish Date". A company is matched by its KAP code, not by name, so renamed companies
  keep one `CompanyId`.

How each column is filled, by KAP statement type (the items are KAP's export column names):

| Column | GENERAL / HOLDING | BANKS / PAR-BANKS | INSURANCE | FINANCE (leasing, factoring) |
|---|---|---|---|---|
| Revenue | Revenue + finance-sector revenue (holding: Total Revenue) | Gross profit from operating activities | Non-life + life + pension technical income | Operating income |
| OperatingIncome | Profit from operating activities | Net operating income | – | Net operating profit |
| NetIncome | Profit attributable to owners of parent (fallback: net profit) | same | same | same |
| TotalAssets / Equity | Total assets / Total equity | same | same | same |
| TotalLiabilities | Total liabilities (fallback: current + non-current) | Equity & liabilities − equity | Equity & liabilities − equity | Equity & liabilities − equity |
| Current / NonCurrentLiabilities | ✔ | – | ✔ | – |
| CashAndEquivalents | – | – | – | Cash & central bank balances |
| PeriodEndDate | quarter end of the fiscal year (June-start fiscal years for football clubs, see `FISCAL_YEAR_START_MONTH`) | | | |

- **Revenue** adds "Revenue" and "Revenue from Finance Sector Operations". They are separate
  lines of the same income statement, and both are non-zero in only a few statements, e.g.
  a brokerage with trading and finance income. Taking just one would under-report those.
- **NetIncome** is "Profit (Loss) Attributable To, Owners of Parent", the figure EPS and P/E
  use. It falls back to "Net Profit (Loss)" when a statement has no split, e.g. a solo
  statement without subsidiaries.

**Not published by this KAP endpoint, left empty:** `Ebitda`, `TotalDebtShort`,
`TotalDebtLong`, `CashAndEquivalents` (non-finance), `FreeCashFlow`, `Eps`,
`SharesOutstanding`. The item search offers only 29 items for ordinary companies: the
income-statement totals, assets, liabilities and equity. It has no depreciation, borrowings,
cash, cash flow, EPS or share-count items. `fundamental_reports` fills them from each
statement's full financial report (next section).

Companies appear in KAP's export under the name they had when filing. Renamed companies are
matched by name similarity, and otherwise by asking KAP about half the batch at a time. The
result is remembered in `cache/fundamentals/aliases.json`.

### fundamental_reports → fills `output/fundamentals/company_fundamental.csv`

Reads every statement's full financial report, the file behind the "EXCEL" link on its KAP
disclosure page, and writes seven more columns into `company_fundamental.csv` in place.
Run it after `fundamentals`. `all` does this for you.

- Each row's report is found via the `NotificationId` in `company_fundamental_raw.csv`.
  It is the same statement `fundamentals` chose: consolidated before solo, then the latest.
- The lines found in a report are cached in `cache/fundamental_reports/<NotificationId>.json.gz`,
  current period only. A published notification never changes, so the cache is kept forever.
  After a new `fundamentals` run (which rewrites the CSV with these columns empty), this
  command refills everything from the cache and downloads only the new statements.
- `--years` / `--symbols` limit what is **downloaded**. Rows outside them are still filled
  from the cache.
- **Two download routes.** A quarter with 50 or more reports missing (`market_min_missing`)
  is downloaded as one zip of every company's report for that quarter, the file KAP's home
  page gives when you choose a year and quarter but no company. One quarter is ~25 MB and ~770
  reports and takes about 3 minutes. It holds ~99% of the statements `fundamentals` picks.
  The rest (and new filings in later runs) come one at a time from the disclosure's "EXCEL"
  export, which KAP limits to about 10 per 5 minutes (30 s apart).
- The first 10-year run takes ~30 minutes for the 35 quarter files (about 20,000 reports), then
  ~6 hours for the ~450 statements the files lack (~2%, mostly 2016–2020, one every ~45 s).
  KAP often cuts a quarter file after 3–5 minutes; a retry a few minutes later usually gets
  it at once (up to 5 tries). It goes newest quarter first, can be stopped
  at any time (Ctrl-C or closing the terminal fills in what is downloaded), and resumes where
  it stopped.
- Values are in the statement's own unit (`PresentationCurrency`), like the other columns,
  and year-to-date like the income statement (Ebitda, FreeCashFlow).
- `output/fundamental_reports/report_lines.csv` lists the report lines behind every value
  (`CompanyId, StockCode, FiscalYear, FiscalQuarter, NotificationId, PresentationCurrency,
  Column, Statement, Line, Value`), so each number can be checked against KAP.

| Column | GENERAL / HOLDING | BANKS / PAR-BANKS | INSURANCE | FINANCE |
|---|---|---|---|---|
| CashAndEquivalents | Cash and cash equivalents | Cash and cash equivalents | Cash and cash equivalents | (from `fundamentals`) |
| TotalDebtShort | Current Borrowings + Current Portion of Non-current Borrowings | – | – | – |
| TotalDebtLong | Long Term Borrowings | – | – | – |
| Ebitda | Profit from operating activities + depreciation and amortisation (cash-flow adjustment) | – | – | – |
| FreeCashFlow | Cash flows from operating activities − purchases of property, plant, equipment and intangibles | – | – | – |
| Eps | NetIncome × unit ÷ SharesOutstanding | same | same | same |
| SharesOutstanding | Issued capital × unit | Issued capital × unit | Paid-in capital × unit | Issued capital × unit |

- **Borrowings include lease liabilities.** KAP's taxonomy puts them under borrowings.
- **Ebitda** stays empty when the cash flow uses the direct method (no depreciation line).
  It is operating profit + D&A, so it includes "other operating income/expenses"; Turkish
  brokers' FAVÖK often excludes those.
- **FreeCashFlow**: when a report has no purchase lines, nothing was bought, so FCF equals
  operating cash flow.
- **Eps** is computed, in TL per share (not in the statement's unit), to 8 decimals:
  `NetIncome` (attributable to owners of the parent) × unit ÷ `SharesOutstanding`. The
  published EPS is not used, because companies publish it in different units: ASELS in kuruş
  (316.65), YKBNK per 0.01 TL of capital (0.0367), most in TL; THYAO, KUYAS and insurers don't
  publish it in the report. The computed value uses period-end shares including treasury
  shares, so it can differ a little from the official weighted-average EPS (EREGL 2026 Q2:
  1.276 vs 1.329).
- **SharesOutstanding** is issued capital in TL at 1 TL nominal per share, the BIST
  standard: `3063214` in `1000TL` gives 3,063,214,000 shares for SISE. It is a count, so the
  unit is applied. It includes treasury shares.
- Financial companies have no EBITDA or free cash flow, and their balance sheets do not
  split debt into short and long term, so those columns stay empty.
- A template without a mapping (for example a new KAP statement type) is logged and left
  empty. Add it to `FIELDS` in `stock_crawler/fundamental_reports/crawler.py`.

### market_index → `output/market_index/`

```
Market (1) ──< MarketIndexMaster (1) ──< MarketIndexData
```

| File | Table | Columns | Example row |
|---|---|---|---|
| `market.csv` | Market | `MarketId, MarketCode, CountryCode, CountryName, BaseCurrency` | `1,BIST,TR,Türkiye,TRY` |
| `market_index_master.csv` | MarketIndexMaster | `IndexId, IndexCode, MarketId, IndexName` | `1,XU100,1,BIST 100` |
| `market_index_data.csv` | MarketIndexData | `TradeDate, IndexId, ClosePrice` | `2026-09-15,1,13892.2998` |

- Each file contains every parent row its child rows reference, so foreign keys always
  resolve. Rows are unique on `(TradeDate, IndexId)`.
- Values are 15 minutes delayed. Before 18:15 Istanbul time, today's value is intraday and
  is left out unless you pass `--include-today`.
- `ClosePrice` is copied digit for digit from the source. Many historical closes are stored
  there as 32-bit floats, so they carry float noise (e.g. `13892.2998` for 13892.30).
- Timestamps are converted to dates in the `Europe/Istanbul` zone (Türkiye used daylight
  saving until 2016). Stray weekend rows that repeat Friday's close are dropped.

### sovereign → `output/sovereign/macro_sovereign_tr.csv`

Columns: `MarketId, AsOfDate, PeriodType, PublishDate, Gdp, TaxRevenue, PublicDebt,
InterestRate, Cpi, FxRateUsd, CdsSpreadBps`.

| Column | Meaning / unit | Monthly → quarterly/annual |
|---|---|---|
| AsOfDate | last day of the period (e.g. 2025-03-31) | – |
| PeriodType | `MONTHLY`, `QUARTERLY` or `ANNUAL` | – |
| PublishDate | the day the crawler ran | – |
| Gdp | thousand TRY (as EVDS publishes it) | sum of quarters |
| TaxRevenue | thousand TRY (as EVDS publishes it) | sum of months |
| PublicDebt | million TRY (as the Treasury publishes it) | end-of-period value |
| InterestRate | percent (38.0000 = 38%) | end-of-period value |
| Cpi | index, 2025 = 100 | average of months |
| FxRateUsd | TRY per 1 USD | last business day of the period |
| CdsSpreadBps | basis points | last day of the period |

- Values are not rounded. EVDS sends 6 decimals (`79227239.000000`). Averages (quarterly and
  annual Cpi) keep every digit of the calculation.
- **Expected empty cells:** `Gdp` in monthly rows (only published quarterly, never split);
  `CdsSpreadBps` unless you pass `--cds-csv`; the newest month of `InterestRate` (BIS
  publishes about a month late). Periods that have not ended, such as the current year's
  ANNUAL row, are never produced.
- The crawler aggregates periods itself and drops any period that is incomplete. EVDS's own
  yearly totals would include partial years. Values out of range (e.g. a negative CPI) are
  logged and dropped. A value too large for its column stops the run.
- History: Gdp from 2000, PublicDebt and InterestRate from 2002, Cpi from 2005, TaxRevenue
  from 2006. 20 years is the most that fills every column.
- Choices: **PublicDebt** is the Treasury's monthly central-government gross debt (EVDS only
  has general-government debt, quarterly from 2015). **InterestRate** is the policy rate, not
  `TP.APIFON4` (the funding cost, which differed in 2023). **Cpi** uses the 2025=100 series;
  the old `TP.FG.J0` stops in 2026-01.
- CDS is paid market data (Bloomberg, LSEG, S&P). Export a daily CSV with `date,value`
  columns and pass it with `--cds-csv` (or the `cds_csv` setting).

## 8. Staying under the servers' limits

Every crawler sends one request at a time and never disables TLS certificate checks. All
except the Treasury download retry 429/5xx with growing waits and honour `Retry-After`.

| Source | Pace | When refused |
|---|---|---|
| KAP pages (`companies`) | 2 s ± 30 % between pages; one kept-alive connection | Doubles the gap (up to 60 s) on every push-back; after a 403/429 it never goes back below 1.5× that rate. It pauses 5 min and retries a company that keeps failing, and stops after 3 pauses in a row. |
| KAP whole-quarter zip (`fundamental_reports`) | one request per quarter, ~3 min each, one after another | a failed or unreadable zip is skipped; its reports are fetched one by one |
| KAP report export (`fundamental_reports`) | 30 s ± 30 % between reports; KAP answers HTTP 429 above ~10 reports per 5 minutes | same as KAP pages, up to a 120 s gap; pauses 10 min and retries a report that keeps failing, and stops after 3 pauses in a row |
| KAP export API (`fundamentals`) | 30–60 s between requests, plus a 30–90 s break every 40 requests; stays within the page's 10 × 2 × 10 limits | Doubles the gap on every refusal and eases back after 25 successes. It waits 10 min after 3 refusals in a row and stops cleanly after 6. It waits for your own network to come back without counting that as a refusal. |
| Borsa İstanbul + TCMB (`market_data`) | 1 s per server; the two servers are asked in turn | same as KAP pages |
| İş Yatırım (`market_index`) | 1.5 s + up to 1 s random; one request per index covers the whole range | 401/403 or an HTML page instead of JSON stops the run at once; stops after 3 failed indices in a row |
| EVDS + Treasury (`sovereign`) | ≥ 1 s; ~10 requests to EVDS and 2 to hmb.gov.tr per run | EVDS: retries 5 times, then stops; Treasury: stops at the first error |

- **Never run two KAP crawlers (`companies`, `fundamentals`, `fundamental_reports`) at the same time.** They share
  KAP's limit and trigger its firewall twice as fast. `all` runs them one after the other.
- Once a day is enough for everything. EVDS updates daily and KAP statements quarterly.
- Caches: `cache/companies` (KAP pages, reused for 7 days), `cache/market_data` (bulletins
  and FX files, kept forever), `cache/fundamentals` (recent years 12 h, older years 30 days),
  `cache/fundamental_reports` (report lines, kept forever).
  Delete a cache folder, or pass `--refresh` to `fundamentals`, to download again.

## 9. Loading into SQL Server

`load_db` copies the CSV files from `output/` into your database:

```bash
python -m stock_crawler load_db
```

Run it after the crawlers, or add `"load_db"` at the end of `run_all` in the config so `all`
crawls and then loads. It reads the files on your machine and sends the rows over the
connection, so the SQL Server can be anywhere. The tables must already exist; `load_db`
never creates, alters or empties a table. For a new, empty database,
`deploy/create_tables.sql` creates them.

### Setup (once)

1. Install Microsoft's ODBC driver. `pyodbc` (in `requirements.txt`) uses it.
   - macOS: `brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release`,
     then `brew install msodbcsql18`
   - Ubuntu: `deploy/setup_ubuntu.sh` does it (see [On Ubuntu](#on-ubuntu-set-up-test-schedule))
   - Windows: the "ODBC Driver 18 for SQL Server" installer from Microsoft
2. Set the server and database in `config/config.toml`. Keep the password out of the file:
   ```toml
   [load_db]
   connection = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=myserver,1433;DATABASE=StockDb;UID=loader;Encrypt=yes;TrustServerCertificate=yes"
   ```
   ```bash
   export DB_PASSWORD='...'        # Windows: set DB_PASSWORD=...
   ```
   With a Windows login, write `Trusted_Connection=yes` instead of `UID=...` and skip the
   password. `TrustServerCertificate=yes` accepts the self-signed certificate most SQL
   Servers have; leave it out if yours has a trusted one.
3. New, empty database: create the tables with `deploy/create_tables.sql` (see
   [On Ubuntu](#on-ubuntu-set-up-test-schedule), step 3). Existing database: check
   `config/db_tables.csv` against it (see below).
4. Try it without changing anything: `python -m stock_crawler load_db --dry-run` loads every
   table, logs the counts and rolls back.

### On Ubuntu: set up, test, schedule

**1. Set up.** In the project folder (e.g. `/opt/stock-crawler`):

```bash
ACCEPT_EULA=Y bash deploy/setup_ubuntu.sh
```

`ACCEPT_EULA=Y` says you accept the licence of Microsoft's ODBC driver
(https://aka.ms/odbc18eula). The script installs Python, the driver (package `msodbcsql18`),
`.venv` with every requirement, and runs the offline tests. Running it again is safe.

**2. Test `load_db` against SQL Server.** `tests/test_load_db_live.py` creates two tables of
its own (`LoadDbTest_Market`, `LoadDbTest_Price`), loads small CSV files into them and
checks inserts, reruns, updates, SourcePriority, rollback after an error, and the dry run.
Then it drops them. Nothing else is touched, but point it at a test database:

```bash
export DB_PASSWORD='...'
export LOAD_DB_TEST_CONNECTION="DRIVER={ODBC Driver 18 for SQL Server};SERVER=myserver,1433;DATABASE=StockDbTest;UID=loader;Encrypt=yes;TrustServerCertificate=yes"
.venv/bin/python -m pytest tests/test_load_db_live.py -v
```

No test database? Start a throwaway SQL Server in Docker on the same machine. This accepts
Microsoft's SQL Server licence. The password needs 8+ characters from three of: upper case,
lower case, digits, symbols. Otherwise the container stops at start-up (`docker logs`
says "password validation failed") and every connection times out:

```bash
export DB_PASSWORD='Choose-A-Str0ng-One'
docker run -d --name mssql-test -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD="$DB_PASSWORD" -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
sleep 30   # first start-up
.venv/bin/python -c "import os, pyodbc; pyodbc.connect('DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost,1433;UID=sa;TrustServerCertificate=yes;PWD=' + os.environ['DB_PASSWORD'], autocommit=True).execute('CREATE DATABASE LoadDbTest')"
export LOAD_DB_TEST_CONNECTION="DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost,1433;DATABASE=LoadDbTest;UID=sa;Encrypt=yes;TrustServerCertificate=yes"
.venv/bin/python -m pytest tests/test_load_db_live.py -v
docker rm -f mssql-test   # when done
```

All 6 tests must pass. If one fails, keep its output: it shows what your SQL Server does
differently.

**Have a backup (`StockDb.bak`)?** Then steps 3–5 are one command, see
[Backup and restore](#backup-and-restore).

**3. Create the tables** (new, empty database only). With SQL Server in a Docker container
named `stock-crawler-mssql` and a database `StockDb`:

```bash
docker cp deploy/create_tables.sql stock-crawler-mssql:/tmp/create_tables.sql
docker exec -e SQLCMDPASSWORD="$DB_PASSWORD" stock-crawler-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -C -b -d StockDb -i /tmp/create_tables.sql
```

Elsewhere, open the file in SSMS or Azure Data Studio and run it in your database. It
creates all eight tables or none.

**4. Check your real tables.** Set `connection` in `[load_db]`, then, with the CSV files in
`output/` (run the crawlers, or copy `output/` from another machine):

```bash
.venv/bin/python -m stock_crawler load_db --dry-run
```

This loads every CSV into your real tables and rolls back. The log names each table that
doesn't fit (a missing table or key column, a foreign key, a type), so you can fix
`config/db_tables.csv`, the IDs, or the table before the first real load.

**5. Load and schedule.** Run `.venv/bin/python -m stock_crawler load_db` once. MarketData
is skipped until you export the CompanyIds the Company table just made (see
[MarketData needs your CompanyIds](#marketdata-needs-your-companyids)). Then add
`"load_db"` at the end of `run_all` in the config, so the daily `all` crawls and loads. Cron
doesn't read your shell profile, so put the password at the top of your crontab
(`crontab -e`; only you and root can read it):

```cron
DB_PASSWORD=your-password
0 20 * * 1-5  cd /opt/stock-crawler && .venv/bin/python -m stock_crawler all >> logs/cron.log 2>&1
```

### `config/db_tables.csv`

One row per table, loaded in this order (parent tables first, for foreign keys):

| Table | Csv | Keys |
|---|---|---|
| `dbo.Market` | `output/market_index/market.csv` | `MarketId` |
| `dbo.MarketIndexMaster` | `output/market_index/market_index_master.csv` | `IndexId` |
| `dbo.MarketIndexData` | `output/market_index/market_index_data.csv` | `TradeDate IndexId` |
| `dbo.Company` | `output/companies/companies.csv` | `MarketId Ticker` |
| `dbo.MarketData` | `output/market_data/market_data_db.csv` | `TradeDate CompanyId` |
| `dbo.KapCompany` | `output/fundamentals/companies.csv` | `CompanyId` |
| `dbo.CompanyFundamental` | `output/fundamentals/company_fundamental.csv` | `CompanyId FiscalYear FiscalQuarter` |
| `dbo.MacroSovereign` | `output/sovereign/macro_sovereign_tr.csv` | `MarketId AsOfDate PeriodType PublishDate` |

`Keys` are the columns that identify a row, separated by spaces: usually the table's
primary key.

**Two kinds of CompanyId.** In Company and MarketData, `CompanyId` is your database's own
ID, made by the Company table. In CompanyFundamental, it is KAP's company code (832, 833,
…), as `fundamentals` writes it. KapCompany lists those codes with their stock codes, so
CompanyFundamental's foreign key points at KapCompany. Pointing it at Company would match
KAP codes to unrelated companies whose own ID happens to be the same number. To combine the
two, join `KapCompany.StockCode` to `Company.Ticker`; a few StockCodes list two codes, e.g.
`A1CAP, ACP`. Edit `Table` if your schema or table names differ. To leave a table out,
delete its row, or pass `--tables` to load only some. A CSV that doesn't exist yet is
skipped with a warning.

### What it does

Each table is loaded in one transaction:

1. The CSV is copied into a temporary table with the table's column types (an empty cell
   is NULL).
2. Rows whose key is already in the table are updated, but only if a value changed.
3. The other rows are inserted.

- **Nothing is deleted, and running it again is safe.** Repeated runs overlap dates you
  already loaded; those rows are left alone unless the CSV has a different value.
- Only the CSV columns that the table has are loaded. The others are named in the log, e.g.
  `PresentationCurrency` if your CompanyFundamental table has no such column. Every key
  column must be in both.
- SQL Server converts the text to each column's type, as `BULK INSERT` would. Values with
  more decimals than the column (index closes with float noise, Cpi averages) are rounded
  to the column's scale.
- A CSV with an empty or repeated key is refused before the table is touched.
- **SourcePriority:** in a table with this column (MarketData), a row never replaces one
  from a preferred source, i.e. one with a lower `SourcePriority`.
- If a table fails (a value that doesn't fit, a foreign key, a missing column), it is rolled
  back and left as it was. The other tables are still loaded, and the exit code is 1.
- `logs/load_db.log` gets one line per table: rows in the file, inserted, updated and
  unchanged. `-v` also logs the SQL statements.

If your tables have foreign keys, the IDs in the CSV files must match them; see
[IDs that must match your database](#ids-that-must-match-your-database).

### MarketData needs your CompanyIds

`market_data.csv` names companies by ticker, but the MarketData table uses your database's
`CompanyId`. `market_data --db-output` writes exactly the table's columns: `TradeDate,
CompanyId, OpenPrice, HighPrice, LowPrice, ClosePrice, Volume, ValueTraded, SourcePriority`.
Every value is checked to fit `decimal(18,4)`, `bigint` and `decimal(24,4)`. Export the IDs
once (and again after new listings), after Company is loaded:

```sql
SELECT Ticker, CompanyId FROM dbo.Company WHERE MarketId = 1;   -- save as config/company_ids.csv
```

With SQL Server in Docker, this writes the file directly:

```bash
{ echo "Ticker,CompanyId"; docker exec -e SQLCMDPASSWORD="$DB_PASSWORD" stock-crawler-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -C -d StockDb -h -1 -W -s "," -Q "SET NOCOUNT ON; SELECT Ticker, CompanyId FROM dbo.Company WHERE MarketId = 1 ORDER BY Ticker"; } > config/company_ids.csv
```

Then switch on `db_output` and `company_ids` in `[market_data]` (or pass them):

```bash
python -m stock_crawler market_data --db-output output/market_data/market_data_db.csv \
    --company-ids config/company_ids.csv --source-priority 1
```

Tickers without a `CompanyId` are left out and listed in the log. A `CompanyId` used by two
tickers is refused, because it would break the `(TradeDate, CompanyId)` key. A lower
`SourcePriority` means a preferred source (`1` for the official bulletin).

### Backup and restore

A backup file (`.bak`) holds the whole database: tables, keys and data. Use one to set up
another server, or to give the data to someone. It restores on SQL Server 2022 or newer.

**Restore** into the Docker SQL Server (here `stock-crawler-mssql`):

```bash
export DB_PASSWORD='...'            # the container's SA password
bash deploy/restore_db.sh           # backups/StockDb.bak; or: bash deploy/restore_db.sh FILE.bak CONTAINER
```

It checks the file against `StockDb.bak.sha256` (a damaged copy is refused), asks before it
replaces a `StockDb` that already has data, and prints the row count of every table. After a
restore, `config/company_ids.csv` from the same machine as the backup matches its CompanyIds.

On another server (e.g. Windows), use SSMS: *Databases → Restore Database… → Device* and pick
the `.bak`. SSMS puts the files in that server's data folder (*Files* page). In T-SQL:

```sql
RESTORE DATABASE StockDb FROM DISK = N'C:\backup\StockDb.bak' WITH CHECKSUM,
    MOVE 'StockDb' TO N'C:\SQLData\StockDb.mdf', MOVE 'StockDb_log' TO N'C:\SQLData\StockDb_log.ldf';
```

Logins are not part of a backup: create users on the new server as needed.

**Back up.** The database uses the SIMPLE recovery model (its data can always be reloaded from
the CSV files), so the log does not grow without log backups. Set it once on a new database:
`ALTER DATABASE StockDb SET RECOVERY SIMPLE`. Then:

```bash
SQLCMDPASSWORD="$DB_PASSWORD" docker exec -e SQLCMDPASSWORD stock-crawler-mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -C -b -Q "BACKUP DATABASE StockDb TO DISK = '/var/opt/mssql/data/StockDb.bak' WITH INIT, CHECKSUM; RESTORE VERIFYONLY FROM DISK = '/var/opt/mssql/data/StockDb.bak' WITH CHECKSUM"
mkdir -p backups && docker cp stock-crawler-mssql:/var/opt/mssql/data/StockDb.bak backups/StockDb.bak
sha256sum backups/StockDb.bak > backups/StockDb.bak.sha256     # macOS: shasum -a 256
```

### By hand, with `BULK INSERT`

If you have access to the SQL Server machine's disk, you can load a file yourself.
`FORMAT = 'CSV'` needs SQL Server 2017+, and `CODEPAGE = '65001'` keeps Turkish characters.
The path is read by the SQL Server machine, not your laptop:

```sql
BULK INSERT dbo.MarketIndexData FROM 'C:\data\market_index\market_index_data.csv' WITH (FORMAT = 'CSV', FIRSTROW = 2, CODEPAGE = '65001');
```

This only inserts. A second run fails on the duplicate keys, or adds duplicate rows if the
table has no key. For repeated loads, use `load_db`.

## 10. Troubleshooting

**KAP: `The handshake operation timed out`, `Server disconnected`, `SSL_ERROR_SYSCALL`, or
HTTP 403 on every request.** KAP's firewall has blocked your IP for a while, usually after
too many requests. The crawler cools down, then stops by itself, and nothing is lost. Wait a
few hours (overnight is safest) and run the same command again; it resumes from the cache.
Don't restart it repeatedly while blocked, since that tends to extend the block. To check
whether you are clear without crawling (`200` means OK):

```bash
curl -s -o /dev/null -m 25 -w "%{http_code}\n" https://www.kap.org.tr/en/api/analysis/compare-items-by-sector/HOLDING
```

**Many "Slowing down" messages.** The crawler is adapting to the server. For `fundamentals`,
start slower with `min_delay = 45` and `max_delay = 90`.

**`CERTIFICATE_VERIFY_FAILED` (macOS).** `companies` and `market_data` then fall back to
`curl` (slower, no keep-alive). Either run `Install Certificates.command` from your
python.org install, or, behind a VPN/proxy/antivirus that inspects HTTPS, export the macOS
trusted roots and point the crawlers at them:

```bash
security find-certificate -a -p /Library/Keychains/System.keychain /System/Library/Keychains/SystemRootCertificates.keychain > ~/macos-roots.pem
```

Then set `ca_bundle = "~/macos-roots.pem"` in `[companies]`, `[market_data]` and
`[sovereign]`. `sovereign` already uses the OS trust store through `truststore`.

**Proxy (e.g. Hiddify).** Set `proxy = "http://127.0.0.1:12334"` (or `socks5h://…`) in
`[companies]` and `[market_data]`. Test it first:

```bash
curl --proxy http://127.0.0.1:12334 --noproxy '' -L -o /dev/null -w 'HTTP %{http_code}\n' https://kap.org.tr/tr/bist-sirketler
```

**`sovereign`: "Series … not found in EVDS datagroup".** EVDS renamed or rebased a series.
Find the new code on the datagroup's page (links in section 6) and update
`stock_crawler/sovereign/fields.py`.

**`fundamentals`: warning about a "non-calendar fiscal year".** A company published a
statement before its computed period end. Add its stock code to `FISCAL_YEAR_START_MONTH` in
`stock_crawler/fundamentals/crawler.py`.

**`load_db`: "Can't open lib 'ODBC Driver 18 for SQL Server'" or "Data source name not
found".** The ODBC driver is not installed, or under another name. The name in `DRIVER={...}`
must be one that `python -c "import pyodbc; print(pyodbc.drivers())"` lists, e.g.
`ODBC Driver 17 for SQL Server`.

**`load_db`: "certificate verify failed" / "SSL Provider".** The server has a self-signed
certificate. Add `TrustServerCertificate=yes` to `connection`.

**`load_db`: "conflicted with the FOREIGN KEY constraint", "Error converting data type
nvarchar to numeric", "would be truncated".** A value doesn't fit your table: an ID that is
not in the parent table (see [IDs](#ids-that-must-match-your-database)), or a column type
too small for the data. That table was rolled back, and the others were loaded. Run with
`-v` to see the SQL.

**`setup_ubuntu.sh`: pip "Could not install packages … files.pythonhosted.org".** The network
blocks PyPI's download server. Install the same libraries from Ubuntu's archive and let the
`.venv` use them:

```bash
sudo apt-get install -y python3-pyodbc python3-pytest python3-httpx python3-openpyxl python3-requests python3-xlrd python3-truststore tzdata
rm -rf .venv && python3 -m venv --system-site-packages .venv && .venv/bin/python -m pytest -q
```

**"Cannot read config/config.toml".** Run the command from the project folder, or pass
`--config PATH`.

## 11. Changing or adding sources

| To change | Edit |
|---|---|
| a source address | `config/config.toml` |
| how much history | `years` in `config/config.toml`, or `--years` |
| which indices are fetched, and their IDs | `config/indices.csv` (and `config/markets.csv`) |
| which EVDS series feeds a macro column | `stock_crawler/sovereign/fields.py` |
| how KAP items map to CompanyFundamental columns | `FIELD_MAP` in `stock_crawler/fundamentals/crawler.py` |
| how full-report lines map to the 7 extra columns | `FIELDS` in `stock_crawler/fundamental_reports/crawler.py` (no new downloads needed) |
| which CSV goes into which SQL Server table, and its key | `config/db_tables.csv` |

To add a new crawler, create `stock_crawler/<name>/crawler.py` with
`parse_args(argv)` and `main(argv) -> int`. Add it to `COMMANDS` in `stock_crawler/cli.py`
and add a `[<name>]` section to the config. Write to `output/<name>/`, cache in
`cache/<name>/`.

## 12. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests are offline. They cover the config and command line, including a check that every
key in the shipped `config.toml` is accepted by its command, how `years` becomes a period,
the sovereign pipeline, and `load_db` (against a fake database cursor, so no SQL Server or
ODBC driver is needed).

`tests/test_load_db_live.py` tests `load_db` against a real SQL Server. It is skipped unless
`LOAD_DB_TEST_CONNECTION` is set; see [On Ubuntu](#on-ubuntu-set-up-test-schedule).
